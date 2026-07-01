# Demo recording for podpull.
# Generate the GIF with:  brew install vhs && vhs docs/demo.tape
# Produces docs/demo.gif — then embed it near the top of README.md.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1120
Set Height 660
Set Padding 20
Set Theme "Catppuccin Mocha"

Type "podpull search ""睡前故事"""
Enter
Sleep 4s

Type "podpull get 1532755821 --latest 1"
Enter
Sleep 22s      # resolve feed + download one episode (progress bar) + saved path

Sleep 3s

# --- Optional: show the interactive picker instead of --latest ---
# Requires generous Sleeps so keys land after the picker renders.
# Type "podpull get 1532755821"
# Enter
# Sleep 7s
# Down@600ms 2
# Space
# Enter
# Sleep 20s
