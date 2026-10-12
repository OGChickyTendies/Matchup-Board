# Matchup Sheet

Weekly NFL starters, the defense each one faces, and how that defense ranks against the run and the pass, with injuries and game-day weather.

This repo is deployed by Netlify. `index.html` is updated automatically by scheduled Claude tasks (Wednesday weekly rebuild, daily practice reports, game-day inactives, and post-game scores every 30 minutes during game windows), and Netlify redeploys on every commit.

`tools/points.py` writes fantasy points (FP) and box scores (BOX) from nflverse player stats. `tools/sleeper.py` writes final scores (SCORES) read from Sleeper.
