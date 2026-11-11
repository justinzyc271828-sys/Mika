# Baidu Netdisk authorization and limits

This is the official Open Platform / PCS route.  It is not a browser-cookie
automation and does not reuse the phone’s Baidu session.

## 1. Create an application

1. Register at the Baidu Netdisk Open Platform and create an app.
2. Enable **网盘基础服务** and the upload permission.
3. Copy the **AppKey** and **SecretKey**.
4. Use `oob` as the redirect URI for manual code exchange, or register an exact
   callback URL.
5. Request/activate **文件分享服务** if you need programmatic share links.  This
   is a paid capability; without it, share creation legally cannot succeed.

## 2. Authorize in the app

Settings → 百度网盘:

1. Enter AppKey, AppSecret, app name (the exact `/apps/{appname}` segment), and
   redirect URI.
2. Click **生成授权链接**.  Open it in a browser while signed in to the intended
   Baidu account.
3. Approve the requested scopes (`basic`, `netdisk`).
4. Copy the returned `code` (Baidu shows it for `oob`) into the app and click
   **换取令牌**.

The code is single-use and expires after ten minutes.  The refresh token is
long-lived; access tokens are refreshed 60 seconds before expiry.

## 3. Effective upload root

Official upload/create calls are restricted to the app directory:

```
/apps/{appname}/爱做题的大肥鱼_商品库/{SKU}_{商品名}/
  01_习题详解/    full + quick PDFs
  02_商品图片/    seven PNGs
  03_商品信息/    商品信息.json, 商品介绍.md, 商品图片清单.json
  04_原始教材/    optional, only if include_source=true
```

A root outside `/apps/{appname}` is rejected by the client and by Baidu.  This is
an API limitation, not a missing feature.  If the console grants broader scope,
the code still guards the configured effective root rather than writing anywhere.

## 4. Upload mechanics

- Precreate sends `size`, `isdir=0`, and a JSON array of 4 MB block MD5s.
- The upload host comes from `locateupload`; it is never hardcoded.
- Only missing block indexes are uploaded via `superfile2`.
- `create` sends the ordered block list and `uploadid`; `rtype=3` overwrites an
  existing path after a successful upload.
- The client records `UploadRecord` per file.  A cloud size match yields
  `verified` only when download hashing is enabled; otherwise the evidence text
  explicitly says the listing hash is not the local MD5.

## 5. Share links

`apaas/1.0/share/set` needs the paid share service.  On success the app stores
the URL/password/period as `created_unverified`.  It probes reachability, but a
reachability probe is not proof that a buyer can open it with the intended
permission.  The share is marked verified only by an explicit human check
(`record_manual_verification`).  Cleanup requires a verified share.

## 6. Errors you may see

| errno | Meaning | App behavior |
| --- | --- | --- |
| `-6` | invalid/expired token | `needs_auth`; re-authorize |
| `-7` | path/permission denied | remains an upload error; check `/apps/{appname}` and permission |
| `-8` | already exists | treated as success for folder creation |
| `31066` / share permission errors | share service not purchased/enabled | `needs_manual`; no fake link |
| `42000` | share permission/parameter rejection | `needs_manual` |
| network/5xx | transient | retryable error message |

No uploaded file, directory, share, or hash is reported as successful unless the
API response and the local verification support it.
