# DSD Clear Skies feed

Builds `clearsky.json`, the data file behind https://darkskydispatch.com/clear-skies-tonight/

- Source: NOAA National Weather Service forecast API (public domain, no key).
- Runs every 3 hours on GitHub Actions and publishes to GitHub Pages:
  https://darksky-commits.github.io/dsd-clearsky/clearsky.json
- Status page: https://darksky-commits.github.io/dsd-clearsky/
- If a run gets data for fewer than 70 percent of the points, it fails and the last good feed stays live.
- Run by hand: Actions tab, Update clear sky feed, Run workflow.
