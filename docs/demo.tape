# Demo recording for podpull.
# Generate the GIF with:  brew install vhs && vhs docs/demo.tape
# Produces docs/demo.gif — embedded near the top of README.md.
#
# Flow: search -> list -> get. The `get` uses a small (~8 MB) episode so the
# download completes quickly on screen (no long/stuck progress bar).

Output docs/demo.gif

Set Shell "bash"
Set FontSize 22
Set Width 1200
Set Height 720
Set Padding 22
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms

Type 'podpull search "科幻新闻早知道"'
Enter
Sleep 3s

Type 'podpull list 1848022393 --match "卫星"'
Enter
Sleep 3s

Type 'podpull get 1848022393 --latest 1 --no-input'
Enter
Sleep 16s      # ~8 MB download: resolve + progress bar to 100% + saved path

Sleep 2s
