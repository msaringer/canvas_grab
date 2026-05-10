# canvas-grab

**Looking for Maintainers**

*As I no longer have access to Canvas systems, this project cannot be actively maintained by me. If you are interested in maintaining this project, please email me.*

Grab all files on Canvas LMS to local directory.

*Less is More.* In canvas_grab v2, we focus on stability and ease of use.
Now you don't have to tweak dozens of configurations. We have a very
simple setup wizard to help you get started!

For legacy version, refer to [legacy](https://github.com/skyzh/canvas_grab/tree/legacy) branch.

## Features

- **Download all course files** - Automatically sync all files from Canvas to your local directory
- **Download Canvas pages** - Download actual HTML page content (not just redirects) with proper timestamps
- **Archive external resources** (`--fetch-external`) - For pages already downloaded, follow the external links/embeds and save articles as self-contained HTML and videos as `.mp4` with embedded subs + `.vtt` sidecars, then rewrite links to local copies
- **Smart sync** - Only downloads new or modified files
- **Resume support** - Interrupt and resume downloads at any time
- **Flexible organization** - Choose between file-based or module-based organization
- **File filtering** - Select which file types to download

## Archiving external resources

Many Canvas pages link out to required readings on the open web (Forbes, HBR, etc.) and embed videos from YouTube/Vimeo. After a normal sync, run:

```bash
uv run canvas_grab --fetch-external
```

For each `*.html` page already on disk, this will:

- save external articles as a single self-contained `.html` file using [SingleFile](https://github.com/gildas-lormeau/single-file) running in Docker (`capsulecode/singlefile`) — a real headless Chrome that captures only what the page actually renders, producing dramatically smaller archives than static fetchers
- download PDFs (and other binaries — `.doc(x)`, `.ppt(x)`, `.xls(x)`, `.zip`, `.epub`, `.mp3`, `.csv`, `.rtf`) directly via `requests`, preserving the original extension
- save YouTube/Vimeo/etc. videos as `.mp4` (with embedded subs and a `.vtt` sidecar) via [`yt-dlp`](https://github.com/yt-dlp/yt-dlp); the filename includes the video title
- store everything in a sidecar folder named `<page>_external/`
- rewrite the `<a href>` / `<iframe src>` in the original Canvas page to point at the local copies (canvas-internal links are left alone — they're handled by the normal sync)

Re-running is idempotent: existing files are skipped and only previously-failed URLs are retried. The original page's mtime is preserved so the next normal sync won't think the page has changed.

Prerequisites:

```bash
brew install --cask docker    # or install Docker Desktop manually, and start it
brew install yt-dlp           # or: pipx install yt-dlp
```

The `capsulecode/singlefile` image is pulled automatically on first use.

Add `--fetch-external-verbose` to see the underlying SingleFile / `yt-dlp` output.

Use `--fetch-external-exclude` to skip URLs on specific domains (matches the host or any subdomain). Repeatable and comma-separated, e.g.:

```bash
uv run canvas_grab --fetch-external --fetch-external-exclude sfu.ca,youtube.com
```

Or enable it permanently in `config.toml` so a normal `uv run canvas_grab` runs the external archive automatically after each Canvas sync:

```toml
[fetch_external]
enabled = true
exclude_domains = ["sfu.ca"]
```

CLI excludes are added to (not replacing) the ones in `config.toml`. The `--fetch-external` CLI flag still works on its own and ignores `enabled` (it always runs only the external fetch, no sync).

### Limiting the run to one module

Use `--fetch-external-only PATTERN` (repeatable, comma-separated) to process only HTML files whose path contains the substring (case-insensitive). Handy when iterating on a single module:

```bash
uv run canvas_grab --fetch-external --fetch-external-only "Module 9"
```

Or persistently in `config.toml`:

```toml
[fetch_external]
only_paths = ["Module 9"]
```

### Leave Canvas pages untouched (`--no-rewrite-links`)

By default, the original Canvas HTML is rewritten so links and embeds point at the local copies. If you want the downloads as a *reference library* and prefer to leave the Canvas pages alone (they keep their original `<a href>`s), use:

```bash
uv run canvas_grab --fetch-external --no-rewrite-links
```

Or in `config.toml`:

```toml
[fetch_external]
rewrite_links = false
```

### Where the source URL lives

Whatever the rewrite mode, the source URL of every archived file is preserved in three independent places:

- **HTML archives** — SingleFile embeds a top-of-file comment (`url: https://…`) in every saved page.
- **Videos** — yt-dlp's `--add-metadata` writes the source URL into the MP4's metadata tags (visible in `ffprobe`, Finder Get Info, etc.).
- **Sidecar index** — every `<page>_external/` folder contains a `_sources.json` mapping each archived filename to `{url, kind, fetched_at}`. Updated incrementally on each run.

So even if you copy a downloaded PDF or video off to another folder, you can still trace back to where it came from.

### Driving a real Chrome (`--with-chrome`)

The default Docker SingleFile is a vanilla headless Chromium and gets blocked by sites that fingerprint headless browsers (Cloudflare, Akamai, etc.). Switching to a real Chrome instance with a persistent profile defeats most of those challenges, and lets you preinstall extensions like uBlock Origin or accept consent banners once and have them stick.

Add to `config.toml`:

```toml
[fetch_external]
with_chrome = true
chrome_user_data_dir = "~/.canvas_grab/chrome-profile"
# chrome_executable = ""     # auto-detected; override only if needed
# chrome_remote_port = 0     # 0 = pick a free port
```

Or pass `--with-chrome` (and optionally `--chrome-user-data-dir PATH`) on the CLI.

Prerequisites for this mode:

```bash
npm install -g single-file-cli
# Chrome (or Chromium) installed at a standard location
```

**One-time profile setup:** open Chrome at the same data dir manually so you can install extensions and accept any standing cookie banners — they'll persist across runs:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir=~/.canvas_grab/chrome-profile
```

Install uBlock Origin (or whatever else you want), close Chrome, then run `--fetch-external`. canvas_grab will spawn Chrome at that profile, run all fetches, and shut it down when finished.

## Getting Started

1. Install Python
2. Download canvas_grab source code. There are typically three ways of doing this.
   * Go to [Release Page](https://github.com/skyzh/canvas_grab/releases) and download `{version}.zip`.
   * Or `git clone https://github.com/skyzh/canvas_grab`.
   * Use SJTU GitLab, see [Release Page](https://git.sjtu.edu.cn/iskyzh/canvas_grab/-/tags), or
     visit https://git.sjtu.edu.cn/iskyzh/canvas_grab
3. Run `./canvas_grab.sh` (Linux, macOS) or `.\canvas_grab.ps1` (Windows) in Terminal.
   Please refer to `Build and Run from Source` for more information.
4. Get your API key at Canvas profile and you're ready to go!
5. Please don't modify any file inside download folder (e.g take notes, add supplementary items). They will be overwritten upon each run.

You may interrupt the downloading process at any time. The program will automatically resume from where it stopped.

To upgrade, just replace `canvas_grab` with a more recent version.

If you have any questions, feel free to file an issue [here](https://github.com/skyzh/canvas_grab/issues).

## Build and Run from Source

First of all, please install Python 3.8+, and download source code.

### Using uv (Recommended)

This project uses [uv](https://github.com/astral-sh/uv) for fast Python package management.

1. Install uv:
   ```bash
   # macOS and Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh

   # Windows
   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

2. Run canvas_grab:
   ```bash
   # macOS and Linux
   uv run canvas_grab

   # Windows (in Powershell)
   uv run canvas_grab
   ```

### Using the convenience scripts

We have prepared simple scripts to automatically install dependencies and run canvas_grab.

For macOS or Linux users, open a Terminal and run:

```bash
./canvas_grab.sh
```

For Windows users:

1. Right-click Windows icon on taskbar, and select "Run Powershell (Administrator)".
2. Run `Set-ExecutionPolicy Unrestricted` in Powershell.
3. If some courses in Canvas LMS have very long module names that exceed Windows limits (which will causes "No such file" error
   when downloading), run the following command to enable long path support.
   ```
   Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Type DWord -Value 1
   ```
4. Open `canvas_grab` source file in file browser, Shift + Right-click on blank area, and select `Run Powershell here`.
5. Now you can start canvas_grab with a simple command:
    ```powershell
    .\canvas_grab.ps1
    ```

## Configure

The setup wizard will automatically create a configuration for you.
You can change `config.toml` to fit your needs. If you need to
re-configure, run `./configure.sh` or `./configure.ps1`.

## Canvas Page Downloads

canvas_grab now downloads actual HTML page content from Canvas instead of creating redirect files.

**How it works:**
- Pages are downloaded with their full HTML content
- Timestamps are preserved for proper sync detection
- If a page has no content or cannot be fetched, a redirect link is created as a fallback
- Downloaded pages are saved as `.html` files in the `pages/` folder (file mode) or within module folders (module mode)

**Benefits:**
- Access page content offline
- Pages update only when content changes (using Canvas timestamps)
- No need for internet connection to view downloaded pages

**Note:** The first sync after upgrading will re-download all pages as the content changes from redirect files to actual HTML.

## Common Issues

* **Acquire API token** Access Token can be obtained at "Account - Settings - New Access Token".
* **SJTU users** 请在[此页面](https://oc.sjtu.edu.cn/profile/settings#access_tokens_holder)内通过“创建新访问许可证”按钮生成访问令牌。
* **An error occurred** You'll see "An error occurred when processing this course" if there's no file in a course.
* **File not available** This file might have been included in an unpublished unit. canvas_grab cannot bypass restrictions.
* **No module named 'canvasapi'** You haven't installed the dependencies. Follow steps in "build and run from source" or download prebuilt binaries.
* **Error when checking update** It's normal if you don't have a stable connection to GitHub. You may regularly check updates by visiting this repo.
* **Reserved escape sequence used** please use "/" as the path seperator instead of "\\".
* **Duplicated files detected** There're two files of same name in same folder. You should download it from Canvas yourself.

## Screenshot

![image](https://user-images.githubusercontent.com/4198311/108496621-4673bf00-72e5-11eb-8978-8b8bdd4efea5.png)

![gui](https://user-images.githubusercontent.com/4198311/113378330-4e755300-93a9-11eb-81a9-c494a8cc7488.png)

## Contributors

See [Contributors](https://github.com/skyzh/canvas_grab/graphs/contributors) list.
[@skyzh](https://github.com/skyzh), [@danyang685](https://github.com/danyang685) are two core maintainers.

## License

MIT

Which means that we do not shoulder any responsibilities for, included but not limited to:

1. API key leaking
2. Users upload copyright material from website to the Internet
