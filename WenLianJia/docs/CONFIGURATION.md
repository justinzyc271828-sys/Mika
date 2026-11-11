# Configuration reference

Settings live in `<data root>/03_系统配置/settings.json`.  Defaults are defined
in `core/config.py`; unknown/missing keys are deep-merged on load.  Secrets are
not in this file: they are entered in the GUI and stored via DPAPI in
`secrets.bin`.

## `openai`

| Key | Default | Meaning |
| --- | --- | --- |
| `base_url` | `""` | blank = official OpenAI endpoint |
| `model` | `gpt-4.1` | primary solver model |
| `review_model` | `""` | reviewer model; blank = `model` |
| `escalation_model` | `""` | optional second reviewer |
| `api_mode` | `responses` | `responses`, then Chat Completions fallback |
| `timeout_seconds` | 240 | per-request timeout |
| `max_retries` | 3 | rate-limit/server retries |
| `max_concurrency` | 4 | pipeline solve/verify workers |
| `max_output_tokens` | 4500 | per answer |
| `max_images` | 2 | page images attached per question |
| `temperature` | 0.2 | solver temperature |
| `verify_enabled` | true | run independent review |
| `auto_escalate` | false | use escalation model on disagreement |
| `prompt_version` | `1.0` | written to solutions, used for reuse |

The key is stored under secret name `openai_api_key`.  Environment fallback
`OPENAI_API_KEY` is supported for development only; the GUI path is preferred.

## `budget`

`enforce` (default false), `currency` (default USD), `per_book`, `daily`, and
`prices`.  Prices are a map:

```json
{
  "gpt-4.1": {"input_per_million": 2.0, "output_per_million": 8.0}
}
```

If a model has no configured price, `solutions.cost` is 0 and
`cost_known=false`.  The dashboard shows unknown-cost calls separately; the app
does not guess a vendor price.

## `deepseek`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | false | enable Search/Fetch |
| `base_url` | `https://api.deepseek.com` | API base |
| `search_path` | `/v1/tools/search` | search endpoint |
| `fetch_path` | `/v1/tools/fetch` | page-fetch endpoint |
| `timeout_seconds` | 60 | per-request timeout |
| `max_retries` | 2 | retry 408/429/5xx only |
| `search_count` | 10 | results per search (1–50) |
| `need_content` | false | include page content in search |
| `cache_days` | 30 | metadata cache lifetime |
| `min_authority` | 2.0 | minimum authority score |
| `min_score` | 1.5 | minimum rerank score |
| `max_candidates` | 8 | candidate cap |
| `search_price_per_1000` | 0.75 | USD per 1,000 successful searches |
| `fetch_price_per_1000` | 0.75 | USD per 1,000 successful fetches |

The key is stored under `deepseek_api_key`; it is independent of
`openai_api_key` and is never sent to OpenAI.  Every successful call records
`api_usage.kind` as `deepseek_search` or `deepseek_fetch`; an unset/zero price
produces `cost_known=false`.  Failed requests and empty searches are not billed.

## `books`

`auto_lookup` (true), `allow_manual_candidate` (true), `cover_min_width` (300),
`cover_min_height` (400), `max_cover_bytes` (12,000,000), `title_similarity`
(0.6), `require_verified_cover` (false).  Cover download verifies MIME type,
byte size, and pixel dimensions.  Ambiguous candidates are `NEEDS_REVIEW`; the
GUI never auto-selects a merely similar edition.

## `watermark`

`text`, `font`, `font_index`, `font_size`, `color`, `opacity`, `rotation`,
`position`, `tile`, `tile_gap`.  Defaults: `全网同名爱做题的大肥鱼`, Microsoft YaHei,
42 px, `#9AA0A6`, 0.18, 35°, center, no tiling.

## `latex`

`engine` (`xelatex`), `xelatex_path` (blank = auto-detect), `font_main`,
`font_latin`, `font_song`, `font_hei`, `paper`, `margin_mm`, `compile_timeout`,
`keep_aux`, `toc`, `quick_answer_edition`.

Auto-detection checks the configured path, `PATH`, the bundled TeX Live path
used on this machine (`D:\Workspaces\Tool\TeXLive\bin\windows\xelatex.exe`),
`C:\texlive\2024|2025`, and `TEXLIVE_BIN`.

## `pipeline`

`max_concurrency`, `auto_verify`, `reuse_by_hash`, `stop_on_error`,
`sample_pages`, `ocr_dpi`, `render_dpi`, `min_text_chars`.  The sample page
count is for inspection previews; production parses all pages.

## `netdisk`

`app_key`, `app_name`, `app_id`, `pcs_appid` (250528), `redirect_uri` (`oob`),
`root_dir` (`/爱做题的大肥鱼_商品库`), `create_product_dirs`, `share_period`,
`share_password_mode`, `share_password`, `upload_chunk_mb` (4),
`verify_download_hash`, `auto_upload`, `auto_share`, `include_source`.

`effective_root` is `/apps/{app_name}` + `root_dir` unless `root_dir` already
starts with `/apps/`.  The app secret is stored under `baidu_app_secret`; the
OAuth token JSON under `baidu_token`.

## `cleanup`

`enabled`, `stage_days`, `delete_source`, `require_cloud_verification`.  Defaults
are conservative: disabled, 7 days, keep source, require cloud verification.

## `ui` / `logging`

Window size (1440×900 logical), theme, language; log level, rotation size,
backup count.  Changing the data root requires `WLJ_DATA_DIR`; the setting is
intentionally environment-driven so the GUI cannot silently relocate a
production library.
