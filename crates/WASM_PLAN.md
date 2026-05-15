# LiteParse WASM/Browser Support — Implementation Plan

This document describes how to add browser/WASM support to LiteParse, producing an npm package that runs entirely in the browser with no native dependencies.

## Goal

```js
import init, { LiteParse } from '@llamaindex/liteparse-wasm';

await init(); // load .wasm binary

const parser = new LiteParse({ ocrEnabled: false });
const result = await parser.parse(pdfBytes); // Uint8Array input
console.log(result.text);
```

Users install via npm, import in browser JS/TS, and parse PDFs from `Uint8Array` input. No file paths, no CLI, no LibreOffice/ImageMagick.

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│  Browser JS                                     │
│                                                 │
│  @llamaindex/liteparse-wasm (npm package)       │
│  ┌───────────────────────────────────────────┐  │
│  │ JS glue (from wasm-pack)                  │  │
│  │  - init() loads .wasm                     │  │
│  │  - LiteParse class wrapper                │  │
│  │  - Optional: tesseract.js OCR callback    │  │
│  └────────────────┬──────────────────────────┘  │
│                   │ wasm-bindgen                 │
│  ┌────────────────▼──────────────────────────┐  │
│  │ liteparse-wasm.wasm                       │  │
│  │  - liteparse-wasm crate (Rust)            │  │
│  │  - liteparse core (extract, project, ocr) │  │
│  │  - pdfium (statically linked .a)          │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

PDFium is statically linked into the single `.wasm` binary — one `init()` call, one module.

## Prerequisites

- PDFium WASM build must produce a static archive (`libpdfium.a`) instead of a standalone `.wasm` module. **This change has been made** in `pdfium-binaries` — the `em++` linking step in `steps/06-build.sh` was removed and `steps/07-stage.sh` now stages the `.a` file directly. Once this is built and published as a new release, the `pdfium-sys` crate can consume it.

---

## Changes Required

### 1. `crates/pdfium-sys/build.rs` — Static linking for WASM

The build script currently always links dynamically (`rustc-link-lib=dylib=pdfium`). For wasm32 targets, it needs to link statically and skip the dylib copy step.

**Changes:**
- When `CARGO_CFG_TARGET_ARCH == "wasm32"`:
  - Emit `cargo:rustc-link-lib=static=pdfium` instead of `dylib=pdfium`
  - Skip `copy_dylib_to_target_deps()` (no dynamic loading in WASM)
  - Skip `fix_dylib_install_name()` (macOS-only concern)
- The auto-download logic already maps `wasm32-unknown-unknown` to `pdfium-wasm` — this should continue to work once the new release with the `.a` file is published

**Key code location:** `crates/pdfium-sys/build.rs` lines 27-35 (linking), line 50 (dylib copy)

### 2. `crates/liteparse/` — Feature-gate and WASM compatibility

#### 2a. Feature-gate non-WASM modules

Add `#[cfg(not(target_arch = "wasm32"))]` gates to modules that can't work in the browser:

In `src/lib.rs`:
```rust
pub mod config;
pub mod extract;
pub mod ocr;
pub mod ocr_merge;
pub mod output;
pub mod parser;
pub mod projection;
pub mod types;

#[cfg(not(target_arch = "wasm32"))]
pub mod conversion;
#[cfg(not(target_arch = "wasm32"))]
pub mod render;
```

- **`conversion`** — requires filesystem + external processes (LibreOffice, ImageMagick). No WASM equivalent.
- **`render`** — uses `image::save()` to write files. Screenshot functionality doesn't make sense in the browser context. The page *rendering* logic used by `ocr_merge` (bitmap rendering via pdfium) is separate and should still work.

#### 2b. Make `OcrEngine` trait async

The current trait is sync:
```rust
pub trait OcrEngine {
    fn name(&self) -> &str;
    fn recognize(&self, image_data: &[u8], width: u32, height: u32, options: &OcrOptions)
        -> Result<Vec<OcrResult>, Box<dyn std::error::Error>>;
}
```

Change to async:
```rust
pub trait OcrEngine: Send + Sync {
    fn name(&self) -> &str;
    async fn recognize(&self, image_data: &[u8], width: u32, height: u32, options: &OcrOptions)
        -> Result<Vec<OcrResult>, Box<dyn std::error::Error>>;
}
```

> **Note:** Async traits are stable in Rust 2024 edition (which this project uses). However, `dyn OcrEngine` with async methods requires `async-trait` or manual boxing. Evaluate whether to use `async-trait` crate or `Box<dyn Future>` return types. An alternative is:
> ```rust
> fn recognize(&self, ...) -> Pin<Box<dyn Future<Output = Result<...>> + Send + '_>>;
> ```

**Affected files:**
- `src/ocr/mod.rs` — trait definition
- `src/ocr/tesseract.rs` — `TesseractOcrEngine` impl (wrap sync tesseract-rs call in async)
- `src/ocr/http_simple.rs` — `HttpOcrEngine` impl (convert from `reqwest::blocking` to `reqwest` async)
- `src/ocr_merge.rs` — calls `ocr_engine.recognize()`, needs `.await`
- `src/parser.rs` — calls `ocr_and_merge_pages_from_input()`, already async
- `crates/liteparse-napi/src/lib.rs` — already async, should just work
- `crates/liteparse-python/src/lib.rs` — uses `tokio::runtime::Runtime::block_on()`, should just work

#### 2c. Replace `reqwest::blocking` with async `reqwest`

In `src/ocr/http_simple.rs`:
- Change `reqwest::blocking::Client` to `reqwest::Client`
- Make `recognize()` async (follows from 2b)
- The reqwest async client works both natively (via tokio) and in WASM (via browser `fetch`)

Update `Cargo.toml` dependencies:
```toml
reqwest = { version = "0.13", default-features = false, features = ["json", "multipart", "rustls"] }
# Remove: "blocking" feature
# For WASM, reqwest automatically uses browser fetch — no extra features needed
```

> **Note:** `reqwest`'s `multipart` feature may not be available on WASM targets. Check this — if not, the HTTP OCR engine may need to be `#[cfg(not(target_arch = "wasm32"))]` gated, or use a simpler request format for WASM.

#### 2d. Replace `web_time::Instant`

`web_time::Instant` panics on `wasm32-unknown-unknown`. Use the [`web-time`](https://docs.rs/web-time) crate as a drop-in replacement:

```toml
# Cargo.toml
web-time = "1"
```

```rust
// In parser.rs, replace:
use web_time::Instant;
// With:
use web_time::Instant;
```

`web-time` uses `performance.now()` in WASM and delegates to `web_time::Instant` on native — zero-cost on non-WASM targets.

#### 2e. Conditional dependencies in `Cargo.toml`

```toml
[dependencies]
# Always available
anyhow = "1"
image = { version = "0.25", default-features = false, features = ["png"] }
pdfium = { ... }
pdfium-sys = { ... }
reqwest = { version = "0.13", default-features = false, features = ["json", "multipart", "rustls"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
web-time = "1"

# Native only
clap = { version = "4", features = ["derive"], optional = true }
infer = { version = "0.19", optional = true }
tempfile = { version = "3", optional = true }
tokio = { version = "1", features = ["full"], optional = true }
tesseract-rs = { version = "0.2", features = ["build-tesseract"], optional = true }

[features]
default = ["native"]
native = ["dep:clap", "dep:infer", "dep:tempfile", "dep:tokio", "dep:tesseract-rs"]
tesseract = ["dep:tesseract-rs"]
```

> **Alternative:** Instead of a `native` feature, you could use `#[cfg(not(target_arch = "wasm32"))]` everywhere since the target arch is always known at compile time. The feature approach is more explicit; the cfg approach is less boilerplate. Choose one and be consistent.

### 3. New crate: `crates/liteparse-wasm/`

New wasm-bindgen binding crate, following the same pattern as `liteparse-napi` and `liteparse-python`.

#### `Cargo.toml`
```toml
[package]
name = "liteparse-wasm"
version = "2.0.0"
edition.workspace = true
license.workspace = true

[lib]
crate-type = ["cdylib"]

[dependencies]
liteparse = { path = "../liteparse", default-features = false }
pdfium-sys = { path = "../pdfium-sys" }
pdfium = { path = "../pdfium" }
wasm-bindgen = "0.2"
wasm-bindgen-futures = "0.4"
serde = { version = "1", features = ["derive"] }
serde-wasm-bindgen = "0.6"
js-sys = "0.3"
web-sys = "0.3"
```

#### `src/lib.rs`

Exports via `#[wasm_bindgen]`:

```rust
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub struct LiteParse { /* wraps liteparse::parser::LiteParse */ }

#[wasm_bindgen]
impl LiteParse {
    #[wasm_bindgen(constructor)]
    pub fn new(config: JsValue) -> Result<LiteParse, JsError> { ... }

    /// Parse a PDF from raw bytes. Returns a Promise<ParseResult>.
    pub async fn parse(&self, data: &[u8]) -> Result<JsValue, JsError> { ... }

    /// Format a parse result as JSON or text string.
    pub fn format(&self, result: JsValue) -> Result<String, JsError> { ... }
}
```

**Type conversion:** Use `serde-wasm-bindgen` to convert between Rust structs and JS objects, or define `#[wasm_bindgen]` structs with explicit getters (similar to the napi types pattern). The serde approach is simpler; explicit structs give better TypeScript types.

**OCR callback bridge:** To support user-provided OCR (e.g. tesseract.js), accept a JS function in the config:

```rust
#[wasm_bindgen]
extern "C" {
    pub type JsOcrEngine;

    #[wasm_bindgen(structural, method, catch)]
    pub async fn recognize(
        this: &JsOcrEngine,
        image_data: &[u8],
        width: u32,
        height: u32,
        language: &str,
    ) -> Result<JsValue, JsValue>;
}
```

This lets the JS side provide any OCR implementation:
```js
const parser = new LiteParse({
  ocrEngine: {
    async recognize(imageData, width, height, language) {
      // Use tesseract.js, a remote API, or anything else
      const worker = await Tesseract.createWorker(language);
      const { data } = await worker.recognize(imageData);
      return data.words.map(w => ({
        text: w.text,
        bbox: [w.bbox.x0, w.bbox.y0, w.bbox.x1, w.bbox.y1],
        confidence: w.confidence,
      }));
    }
  }
});
```

The Rust side wraps `JsOcrEngine` into an `OcrEngine` impl that deserializes the JS return value into `Vec<OcrResult>`.

#### Add to workspace

In root `Cargo.toml`:
```toml
[workspace]
members = [
    "crates/pdfium-sys",
    "crates/pdfium",
    "crates/liteparse",
    "crates/liteparse-napi",
    "crates/liteparse-python",
    "crates/liteparse-wasm",
]
```

### 4. New package: `packages/wasm/`

The npm package that users install. Built with `wasm-pack`.

#### Directory structure
```
packages/wasm/
├── package.json
├── src/
│   └── lib.ts          # Optional TS wrapper for ergonomics
├── scripts/
│   └── build.sh        # wasm-pack build script
└── README.md
```

#### `package.json`
```json
{
  "name": "@llamaindex/liteparse-wasm",
  "version": "2.0.0",
  "description": "LiteParse PDF parser for the browser (WASM)",
  "type": "module",
  "main": "./pkg/liteparse_wasm.js",
  "types": "./pkg/liteparse_wasm.d.ts",
  "files": ["pkg/", "README.md", "LICENSE"],
  "scripts": {
    "build": "wasm-pack build ../../crates/liteparse-wasm --target web --out-dir ../../packages/wasm/pkg"
  },
  "keywords": ["pdf", "parser", "wasm", "browser"],
  "license": "Apache-2.0"
}
```

#### Build command
```bash
wasm-pack build crates/liteparse-wasm --target web --out-dir packages/wasm/pkg
```

This produces:
- `pkg/liteparse_wasm_bg.wasm` — the WASM binary (includes pdfium statically linked)
- `pkg/liteparse_wasm.js` — JS glue with `init()` + class exports
- `pkg/liteparse_wasm.d.ts` — TypeScript type definitions

#### Optional: TS wrapper (`src/lib.ts`)

A thin TypeScript wrapper can provide nicer ergonomics (auto-init, better types) similar to `packages/node/src/lib.ts`. This is optional — `wasm-pack` already generates usable JS + `.d.ts` files.

### 5. CI / GitHub Actions

Add a WASM build job to the existing CI. Key steps:

```yaml
- name: Install wasm-pack
  run: cargo install wasm-pack

- name: Build WASM
  run: wasm-pack build crates/liteparse-wasm --target web --out-dir packages/wasm/pkg

- name: Publish to npm
  run: cd packages/wasm && npm publish
```

> **Note:** Ensure the CI runner has the Emscripten SDK if needed for the pdfium static archive build, or pre-download the `pdfium-wasm` release artifact.

---

## API Surface (Browser)

```typescript
// Auto-generated by wasm-pack + any TS wrapper

export function init(): Promise<void>;

export interface LiteParseConfig {
  ocrLanguage?: string;
  ocrEnabled?: boolean;
  maxPages?: number;
  targetPages?: string;
  dpi?: number;
  outputFormat?: "json" | "text";
  preserveVerySmallText?: boolean;
  password?: string;
  quiet?: boolean;
  ocrEngine?: OcrEngine;  // User-provided OCR (e.g. tesseract.js)
}

export interface OcrEngine {
  recognize(imageData: Uint8Array, width: number, height: number, language: string):
    Promise<OcrResult[]>;
}

export interface OcrResult {
  text: string;
  bbox: [number, number, number, number]; // [x1, y1, x2, y2]
  confidence: number;
}

export interface TextItem {
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  fontName?: string;
  fontSize?: number;
  confidence?: number;
}

export interface ParsedPage {
  pageNum: number;
  width: number;
  height: number;
  text: string;
  textItems: TextItem[];
}

export interface ParseResult {
  pages: ParsedPage[];
  text: string;
}

export class LiteParse {
  constructor(config?: LiteParseConfig);
  parse(data: Uint8Array): Promise<ParseResult>;
  format(result: ParseResult): string;
}
```

**Notably absent vs native API:**
- No `parse(filePath: string)` — browser has no filesystem
- No `screenshot()` — no file output; users can render pages themselves if needed
- No `ocrServerUrl` / `tessdataPath` — replaced by `ocrEngine` callback
- No `conversion` support — WASM only handles PDF input

---

## Implementation Order

1. **Make `OcrEngine` async** (affects all crates) — do this first since it touches the most code
2. **Switch `reqwest` from blocking to async** in `HttpOcrEngine`
3. **Replace `web_time::Instant` with `web-time`** in `parser.rs`
4. **Feature-gate `conversion` and `render` modules** for non-WASM
5. **Update `pdfium-sys/build.rs`** for static linking on wasm32
6. **Create `crates/liteparse-wasm/`** with wasm-bindgen exports
7. **Create `packages/wasm/`** with npm package structure (package named `@llamaindex/liteparse-wasm`)
8. **Test**: `wasm-pack build` and verify in a browser
9. **Add CI** for WASM builds and npm publishing

Steps 1-4 should be done carefully with testing on native builds to ensure nothing breaks. Steps 5-7 are WASM-specific and can be iterated on once the new pdfium-wasm release with the `.a` archive is available.

---

## Open Questions

- **WASM binary size**: PDFium is large. The `.wasm` file may be 10-20MB+. Consider whether to document this or explore wasm-opt / size optimization flags.
- **`reqwest` multipart on WASM**: The `multipart` feature of reqwest may not work on wasm32. If HTTP OCR is needed in the browser, may need an alternative HTTP approach or just rely on the JS callback OCR engine.
- **Threading**: WASM is single-threaded by default. The current code is single-threaded anyway, so this should be fine, but `Send + Sync` bounds on `OcrEngine` may need to be relaxed for WASM (`wasm-bindgen` types are `!Send`).
- **Memory**: Large PDFs may stress the WASM linear memory. `ALLOW_MEMORY_GROWTH` is typically enabled by default in wasm-pack builds.
- **`wasm32-unknown-unknown` vs `wasm32-unknown-emscripten`**: The pdfium `.a` is built with Emscripten's clang. This *should* link fine with `wasm32-unknown-unknown` (both target wasm32), but if there are libc/syscall mismatches, may need to target `wasm32-unknown-emscripten` instead. Test this early.
