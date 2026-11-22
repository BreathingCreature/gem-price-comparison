# Known limits

This page records choices that are deliberate. They are not open bugs.

- The reference in `design_ref/` is the visual source only. The app tokens win where they differ, because real data and the required contrast bar are stronger constraints than the static mock.
- `--faint` is darker in light mode and lighter in dark mode than the reference value. The reference value failed the 4.5 to 1 contrast check.
- `.run-screen` uses a zero minimum grid column so long real text cannot force a horizontal scroll at 360 px.
- The matches and System tables scroll inside their cards at 200 percent zoom. Their columns keep the reference widths instead of shrinking to unreadable slivers.
- The top bar is tighter below 400 px so all six controls stay on one row.
- Lane states use short words such as found and blocked so the result column keeps its reference width. The full wording remains in the row title and the System tab.
- Demo mode uses a fixed highlights list and a local placeholder image. It never calls a provider.
- Only the bundled shop domains have local PNG icons. Other shops use the icon route or a globe.
- The low priority highlights client bypasses the router on purpose. The card is optional, so its call must not wait behind listing checks or spend a provider cooldown. It is one call per uncached run.
- Highlights failures become a deterministic list. There is no retry beyond the single call, so the run stays within its time budget.
- An old saved run has no highlights field. The card falls back to its specs.
- A domain seen only in a trace and never in a saved run gets a 204 from the icon route, so the page draws a globe. Every normal run saves its result, so this does not affect normal use.
- Provider and model icons are mapped by name in `frontend/render.js`. New families need a mapping entry; the render tests fail until one is added.
- The GitHub Actions matrix pins Ubuntu 24.04. An extra non blocking Ubuntu latest job warns about runner image changes.
