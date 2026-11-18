<img src="assets/icon.svg" width="64" height="64" alt="">

# games

Created in [T3 Code](https://t3.codes).

## games.asherif.xyz

Play Ludo or Five in a Row with two to four people. Open the hosted page, create
a room, and share its invite link privately. Everyone plays in their browser.
The match is held in the host's tab, so keep it open until you're done.

Build the self-contained page with Go 1.27.1 or later and Python 3.

```sh
bash build-tabletop.sh
```

Upload `dist/index.html` to any HTTPS static host, including Cloudflare Pages.
For local development, serve `dist` over HTTP on localhost.
The smaller files in `dist/cloudflare-preview/` are for Cloudflare temporary
accounts, which limit the upload size per file.

`ludo.py` is the standalone desktop-hosted Ludo version. Run
`python3 ludo.py --tailcat` with the Tailcat CLI installed to use it.
