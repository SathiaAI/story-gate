# Image sources

The README and guide images are plain HTML pages in the story-gate identity (Graphite `#1F2327`, Signal Yellow `#FFD84D`, Paper `#FFF8F2`; small accent text on light uses `#8A6A00`). Edit the HTML, then render it to PNG:

```bash
npm install playwright        # once
node docs/assets/src/render.js docs/assets/src/lifecycle.html docs/assets/lifecycle.png        # 2000 px page at 1.2x
node docs/assets/src/render.js docs/assets/src/setup.html docs/assets/setup.png 1 1600          # setup is 1600 px at 1x
node docs/assets/src/render.js docs/assets/src/header.html header.png && convert header.png -quality 88 docs/assets/header.jpg
```

Set `CHROMIUM=/path/to/chromium` if Playwright's own browser is not installed.

- `theme.css` holds the colours and fonts (Clash Display and Switzer from `.story-gate/vendor/`).
- `header-city.png` is the station illustration recoloured to Graphite ink with a Signal Yellow buffer stop.
- The raven mark (`../story-gate-mark.svg`, `../story-gate-mark-small.svg`) is artwork from the Viaknox brand kit. Do not redraw, recolour or crop it. Use the small mark below 64 px.
