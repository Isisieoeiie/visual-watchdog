# visual-watchdog

Screenshot a set of web pages on a schedule, compare each against an approved
baseline, and get a visual report of anything that actually changed — without
drowning in false alarms from animations, web fonts, or content that shifts a
few pixels.

![A rendered diff: unchanged content is greyed out, the five changed elements
are boxed in red, and a known-dynamic banner is masked out entirely](assets/demo-diff.png)

*Baseline vs. a change a designer might ship. The four card headings and the
button were recoloured; the watchdog greys out everything untouched, boxes the
five real changes, and ignores the masked banner (the grey bar) completely.*

## Why this is harder than "diff two PNGs"

The naive version of this tool — screenshot, compare pixels, flag any
difference — is useless within a day. It fires on every animation caught
mid-frame, every web font that loads late and reflows the text under it, every
"2 minutes ago" timestamp, every rotating carousel. People learn to ignore it,
and then it might as well not exist.

So most of the work here is in *not* crying wolf. Two problems in particular
get the attention:

**Everything that makes a page non-deterministic is neutralised before the
shot is taken.** The clock and `Math.random` are frozen before any page script
runs, so anything derived from them renders identically every time. Animations
and transitions are driven to their final frame (not disabled — an element that
fades in from `opacity: 0` would otherwise stay invisible). The capture waits
for the network to go idle *and* for `document.fonts.ready`, then scrolls the
full height to trigger lazy-loaded images and waits for them to decode. Locale,
timezone, colour profile and device scale are all pinned. Known-dynamic regions
(a fundraising banner, a "related articles" strip) are masked out by CSS
selector.

**A pixel-counting diff mistakes a small shift for a total rewrite.** If a
heading grows four pixels, everything below it moves down four pixels, and a
"percentage of pixels changed" metric will scream that 90% of the page is
different. The watchdog detects that case explicitly: it correlates the two
images' row-brightness profiles to recover the vertical offset, re-aligns, and
reports *"content moved 4px down"* separately from genuine change. It handles
the common special case — the page simply got taller — that crashes tools
assuming matching dimensions.

## How comparison works

Each page/viewport pair is compared with **SSIM** (structural similarity), which
tracks perceived structure rather than raw pixel equality, so sub-pixel
rendering noise doesn't register. SSIM runs on luminance, so it's paired with a
per-pixel colour-distance check — otherwise a recolour that preserves brightness
(blue → red, say) would sail through unnoticed. Pixels that differ structurally
*or* in colour are grouped into regions; tiny specks below a size threshold are
dropped, and nearby changed pixels are merged so recolouring a line of text
reads as one finding, not one per glyph.

A comparison ends in one of five states:

| Status      | Meaning                                                        |
|-------------|----------------------------------------------------------------|
| `pass`      | within threshold, nothing moved                                |
| `shifted`   | content moved or the page changed height, but nothing else     |
| `changed`   | a substantive visual difference                                |
| `new`       | a snapshot with no baseline yet — run `approve` to accept it   |
| `error`     | width mismatch, missing snapshot, or a capture failure         |

## Quick start

Requires Python 3.10+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

Then, against the pages listed in `targets.yaml`:

```powershell
# 1. take the first screenshots and accept them as the reference
python -m vwatch capture
python -m vwatch approve

# 2. later — re-screenshot, compare, and build the report
python -m vwatch run
```

`run` (capture + compare in one step) exits `0` when everything passes and `1`
when something needs review, so it drops straight into a scheduled task or CI
step. Open `report/index.html` to see the result: a status tally, per-target
SSIM and changed-area numbers, a baseline / current / difference strip, and an
onion-skin slider that blends between baseline and current.

## Configuring targets

`targets.yaml` lists the pages to watch. Anything under `defaults` applies to
every page unless that page overrides it.

```yaml
defaults:
  viewports:
    - { name: mobile,  width: 390,  height: 844,  mobile: true }
    - { name: desktop, width: 1440, height: 900,  mobile: false }
  threshold: 0.995        # min SSIM to count as unchanged
  max_height: 6000        # cap very tall pages; 0 keeps the whole document

pages:
  - name: example
    url: https://example.com

  - name: wikipedia-python
    url: https://en.wikipedia.org/wiki/Python_(programming_language)
    mask:
      - "#footer-info-lastmod"   # the "last edited" timestamp
      - ".read-more-container"   # a strip of random related-article cards
```

That `.read-more-container` mask is a real example: mobile Wikipedia shows a
different random set of related-article cards on every visit, so without the
mask that page never stops reporting as changed. Masking paints the region a
flat colour in *both* images, so it drops out of the comparison. When a mask
selector matches nothing, the run says so, so stale selectors don't hide silently.

The `max_height` cap matters more than it looks: a documentation page can run to
40,000 pixels, and asking a browser to render and stitch a surface that tall
takes minutes and gigabytes. The capture resizes the viewport to the slice it
keeps rather than rendering the whole document.

## Commands

| Command                    | What it does                                             |
|----------------------------|----------------------------------------------------------|
| `capture`                  | take fresh screenshots into `snapshots/`                 |
| `compare`                  | diff snapshots against `baselines/`, write the report    |
| `run`                      | `capture` then `compare`                                 |
| `approve`                  | promote the latest snapshots to be the new baselines     |
| `publish`                  | copy the report into `docs/` for GitHub Pages            |
| `selftest`                 | prove determinism + detection on the bundled fixture     |

Useful flags: `-c/--config` to point at a different target file, `--only PAGE …`
to limit to specific pages, `-v/--verbose` for debug logging, and — on `capture`
and `run` — `--inject-css` to apply extra CSS before the shot (inline, or
`@path/to/file.css`). Exit codes: `0` all clear, `1` changes found, `2` error.

Baselines live in `baselines/` and are committed to the repo on purpose: a
legitimate redesign then shows up as an image diff in the pull request that
introduces it, and `approve` + a commit is how you sign off on the new look.

## Publishing the report

`publish` copies the report into `docs/`, which is a folder GitHub Pages can
serve directly. Point Pages at the `/docs` folder on your default branch and the
latest report gets a public URL you can link to.

## Reproducing the demo

The diff above is fully reproducible — it uses the bundled fixture page, so no
external site is involved:

```powershell
python -m http.server 8931 --bind 127.0.0.1 --directory dev
# in another shell:
python -m vwatch -c demo.yaml capture
python -m vwatch -c demo.yaml approve
python -m vwatch -c demo.yaml capture --inject-css "@dev/change.css"
python -m vwatch -c demo.yaml compare
```

`--inject-css` restyles the page at capture time without touching the fixture,
which is also a handy way to see what a real change would look like in the
report before it ships.

## Continuous integration

`.github/workflows/watch.yml` wires the watchdog into GitHub:

- **`selftest` on every pull request** — required. Captures the bundled fixture
  twice (must be identical), then injects a recolour (must report `CHANGED`)
  and a vertical nudge (must report `SHIFTED`). It never reads committed
  baselines, so it is meaningful on the first run.
- **`watch` on every pull request** — screenshots the live pages, posts a
  single sticky comment with the status table (updated in place on each push),
  and attaches the HTML report as an artifact. A visual change turns the check
  red so it cannot merge unnoticed.
- **Nightly** (cron) it re-runs against live pages and, if anything drifted,
  opens or updates one issue labelled `visual-regression`.
- Both run inside the pinned `mcr.microsoft.com/playwright/python` container, so
  renders are identical from one run to the next.

You can run the same engine check locally:

```powershell
python -m vwatch selftest
```

**The one thing to know:** browser rendering differs between operating systems,
so baselines must be captured in the *same* environment CI compares in. The
baselines committed here were captured on Windows and are meant for local use;
CI needs its own. Seed them once from the **Actions** tab → **Run workflow** →
tick **approve**, which captures baselines inside the container and commits them
back. After that, PR and nightly runs are meaningful. To reproduce that
environment locally:

```powershell
docker run --rm -v ${PWD}:/w -w /w mcr.microsoft.com/playwright/python:v1.62.0-jammy `
  sh -c "pip install -r requirements.txt && python -m vwatch capture && python -m vwatch approve"
```

The Playwright version in `requirements.txt` and the container tag in the
workflow are pinned together — bump them in lockstep.

## Limitations and future work

- **Authenticated pages** aren't supported yet; every target is fetched
  anonymously. A stored-session option is the obvious next step.
- **Page discovery is manual** — you list URLs. Crawling a site to find pages
  automatically would scale it up.
- **No history** — each run compares against one baseline. Keeping a timeline of
  scores would let it surface slow drift, not just single-run changes.
- Rendering differs subtly between machines and OSes, so baselines are most
  reliable when captured and compared in the same environment (a pinned browser
  container is the usual fix).
