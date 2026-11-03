# efb-telegraph-middleware

[EH Forwarder Bot](https://github.com/ehForwarderBot/ehForwarderBot) middleware that archives
**every shared link** to [Telegraph](https://telegra.ph), so Telegram shows an Instant View and
the content survives when the original is deleted.

Middleware ID: `flechazo.telegraph`

## What it does

| Message | Result |
|---|---|
| WeChat share card (`MsgType.Link`) | The preview link of the card points to the Telegraph page → Instant View in Telegram |
| Text with URLs | `📰 https://telegra.ph/...` appended for each archived URL (max 3) |

* **Extractors**: WeChat Official Accounts (incl. picture posts), Xiaohongshu notes, Weibo
  (long text, retweets, all pictures), and any other page through
  [readability](https://github.com/buriy/python-readability) with lazy-image handling and a
  description fallback for script-rendered pages.
* **Hotlink protected images**: WeChat, Weibo, Xiaohongshu, Bilibili, Zhihu ... refuse requests
  with a `telegra.ph` Referer, so pages opened in a browser show placeholders. Such images
  (detected per image, or known CDNs) are downloaded, compressed (max 1600 px) and committed to a
  public GitHub repository you own, one commit per article. Identical images are uploaded once.
* **Fast**: the page is published within ~1–2 s using the original image URLs (Telegram fetches
  Instant View images server-side, where hotlink checks pass); re-hosting runs in the background
  and edits the same page.
* Long articles are split into chained pages (`下一页 →`), each page ends with source, author,
  publish time, original link and archive time.
* Same URL (tracking parameters stripped) is archived once and reused for `cache_days`.
* Video sites, shops, mini programs, group invites ... are skipped (see `config.py`).
* Never drops or blocks a message on errors; failed links simply keep their original preview.

## Install

```bash
pip3 install git+https://github.com/FlechazoPh/efb-telegraph-middleware.git
```

Enable it in `<profile>/config.yaml`:

```yaml
middlewares:
- flechazo.telegraph
```

Configure `<profile>/flechazo.telegraph/config.yaml` (see [`config.example.yaml`](config.example.yaml)):

```yaml
telegraph_token: "${TELEGRAPH_TOKEN}"   # empty -> an account is created automatically
image_host:
  type: github
  repo: "${TELEGRAPH_IMAGE_REPO}"      # e.g. you/efb-telegraph-assets (auto-created, public)
  token: "${GITHUB_TOKEN}"             # fine-grained token with Contents: write on that repo
```

`${VAR}` / `${VAR:-default}` are expanded from the environment, so secrets can stay in `.env`.

Without an image host (`type: none`) pages are still created; hotlink protected images then only
render inside Telegram.

## Development

```bash
pip install -e . pytest
pytest
```

## License

MIT
