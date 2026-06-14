# grelmicro README demo recording.
#
# Render with: vhs docs/img/demo/demo.tape
# Produces docs/img/demo.gif. Run from the repo root with the demo deps
# installed (fastapi, uvicorn, grelmicro) in the active environment.

Output docs/img/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1180
Set Height 700
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set Padding 24

Type "cat app.py"
Enter
Sleep 3.5s

Type "uvicorn app:app --port 8000 >/dev/null 2>&1 &"
Enter
Sleep 2.5s

Type "# burst of 5 is allowed, then the limiter throttles"
Enter
Sleep 1s

Type "for i in $(seq 1 7); do curl -s localhost:8000/quote; echo; done"
Enter
Sleep 3s

Type "# Kubernetes-ready health probe"
Enter
Sleep 800ms

Type "curl -s -w '%{http_code}\\n' localhost:8000/healthz"
Enter
Sleep 2.5s

Type "kill %1"
Enter
Sleep 800ms
