# trackpad-sign

Turn your Mac's built-in trackpad into a **signature pad with absolute positioning** — like a Wacom tablet, not a mouse.

Sign documents with your fingertip: where your finger lands on the trackpad is where the stroke lands on the canvas. Touch = pen down, lift = pen up.

```
┌─────────────────────┐         SSE (localhost)        ┌──────────────────┐
│  trackpad_sign.py   │ ─────────────────────────────▶ │    index.html    │
│  ctypes ─▶ private  │   pen down/move/up + pressure  │  canvas + export │
│  MultitouchSupport  │                                │  PNG / SVG       │
└─────────────────────┘                                └──────────────────┘
```

## Why

Most "sign with your trackpad" tutorials and web pads just move the system cursor — you end up *drawing* your name with a mouse instead of *signing* it. This tool reads the raw multitouch stream (absolute normalized finger coordinates + contact state + contact area) from the private `MultitouchSupport` framework via `ctypes`, so the whole trackpad surface maps 1:1 onto the canvas:

- **Absolute coordinates** — lift your finger, put it down somewhere else, the stroke starts exactly there
- **Touch = pen down, lift = pen up** — no clicks needed while signing
- **Speed- and pressure-aware stroke width** — fast strokes thin, slow strokes thick, firm press wider
- **Start/stop gate** — press `Space` (or the on-screen button) to arm/disarm; while disarmed, stray touches and palm grazes leave no ink
- **Exports**: auto-cropped transparent PNG (3x), white-background PNG, or true vector SVG
- **Zero dependencies** — one Python file (stdlib only) + one HTML file; listens on `127.0.0.1` only, nothing ever leaves your machine

## Requirements

- macOS with a built-in trackpad (developed and tested on macOS 26 / Apple Silicon; the finger struct layout has been stable across macOS generations — see [Credits](#credits--prior-art))
- Python 3.8+
- Any modern browser

## Usage

```bash
python3 trackpad_sign.py
```

Opens `http://127.0.0.1:8766/` automatically.

1. Press **Space** (or click 「开始书写」) — the status chip turns green
2. Sign with one finger anywhere on the trackpad surface; lift between strokes
3. Press **Space** again to disarm
4. Export 透明PNG / 白底PNG / SVG — the transparent PNG drops straight onto a document signature line

Options:

| Flag | Meaning |
|---|---|
| `--port N` | HTTP port (default 8766) |
| `--aspect W:H` | physical trackpad aspect in mm, e.g. `160:105`, for an undistorted canvas |
| `--firm T` | fallback arm/disarm threshold on contact area (default 1.4) |
| `--no-open` | don't auto-open the browser |

Hands-free check without touching the pad: `curl http://127.0.0.1:8766/selftest` injects one synthetic stroke through the full pipeline (daemon → SSE → canvas).

### Trackpad click as an extra switch

The daemon also listens for system click events (CGEventTap). On most machines this silently requires the **Input Monitoring** permission; when events flow, a trackpad click toggles arm/disarm as an alternative to `Space`. The daemon logs `收到系统点按事件` once clicks arrive. No permission is needed for anything else.

## 中文说明

把 MacBook 触摸板当**绝对坐标数位板**来签文件：手指落在板面哪里，笔就落在画布哪里；触碰即落笔、抬起即收笔，像真笔不像鼠标。

```bash
python3 trackpad_sign.py
```

- 按**空格**（或点「开始书写」）开始；写完再按空格结束，轻滑误触不落墨
- 按得重笔画粗、轻则细，快写细慢写粗
- 导出透明 PNG（自动裁剪放大约 3 倍）/ 白底 PNG / 矢量 SVG
- 单文件 Python（纯标准库）+ 单文件网页，只监听本机回环地址，无任何上传

原理：`ctypes` 直接加载私有框架 `MultitouchSupport`，注册 `MTRegisterContactFrameCallback` 读手指绝对坐标与接触状态，经 SSE 推给本机网页画布。点按开关走系统点按事件监听（多数机器需要「输入监控」权限，未授权时用空格即可，功能不受影响）。

## Privacy

Your signature is sensitive personal data. Everything runs on `127.0.0.1`, there is no telemetry and no network egress. Keep exported files in a local folder and think twice before uploading them anywhere.

## Known limitations

- Built-in trackpad only (`MTDeviceCreateDefault`); external Magic Trackpad would need `MTDeviceCreateList`
- The physical aspect ratio of the trackpad is not always queryable (`MTDeviceGetSensorSurfaceDimensions` returns nothing on some machines) — pass `--aspect` if the canvas feels stretched, or watch the daemon log, which reports observed surface extents in mm while you write
- Private-framework reverse-engineered structs: stable in practice for many years, but not an API contract (see Credits for the lineage)

## Credits & prior art

- [Karabiner-Elements](https://github.com/pqrs-org/Karabiner-Elements) — `MultitouchExtension/MultitouchPrivate.h` finger struct (production-grade reference)
- [geon0814/tap-sound](https://github.com/geon0814/tap-sound) — empirical ctypes offsets on Apple Silicon
- [MatMercer/mactic](https://github.com/MatMercer/mactic) — byte-level layout documentation
- [ZaynJarvis/trackpad-studio](https://github.com/ZaynJarvis/trackpad-studio) — absolute-position trackpad drawing canvas (different focus, no signing workflow)

## License

[MIT](LICENSE)
