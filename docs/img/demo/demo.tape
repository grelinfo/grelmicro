# grelmicro README demo recording.
#
# Prerequisites: bat, jq, starship, and a Nerd Font (FiraCode Nerd Font Mono).
# Uses the bundled starship.toml in this directory (a deliberately generic
# prompt), never the host config, so the GIF leaks nothing about the machine
# it was recorded on. Render from docs/img/demo with the project venv on PATH:
#
#   cd docs/img/demo
#   PATH="../../../.venv/bin:$PATH" vhs demo.tape
#
# Produces docs/img/demo.gif (one level up).

Output ../demo.gif

Require bat
Require jq
Require starship

Set Shell "zsh"
Set FontFamily "FiraCode Nerd Font Mono"
Set FontSize 14
Set Theme "GitHub Dark"
Set Width 1120
Set Height 700
Set Padding 22
Set Margin 18
Set MarginFill "#0d1117"
Set BorderRadius 12
Set WindowBar Colorful
Set TypingSpeed 30ms

Hide
Type "setopt interactive_comments" Enter
Type 'export STARSHIP_CONFIG=./starship.toml' Enter
Type 'eval "$(starship init zsh)"' Enter
Type "clear" Enter
Show

Type "bat --paging=never --style=numbers,header app.py" Sleep 400ms Enter
Sleep 4s

Type "uvicorn app:app --port 8753 >/dev/null 2>&1 &" Sleep 300ms Enter
Sleep 3.5s

Type "# a 5-burst is served, then the limiter throttles" Enter Sleep 700ms
Type "for i in 1 2 3 4 5 6 7; do curl -s localhost:8753/quote | jq -Cc; done" Sleep 300ms Enter
Sleep 3.8s

Type "# Kubernetes-ready health probe" Enter Sleep 700ms
Type "curl -s localhost:8753/healthz | jq -C" Sleep 300ms Enter
Sleep 3s

Hide
Type "kill %1" Enter
