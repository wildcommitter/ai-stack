Output docs/demo.gif
Set Shell bash
Set FontSize 16
Set Width 1180
Set Height 680
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

Hide
Type "cd /home/user/ai-stack && clear" Enter
Show

Type "# ai-stack — local AI on one AMD R9700, supervised by systemd" Sleep 600ms Enter
Sleep 800ms
Type "systemctl --user is-active ai-stack.service kali-sandbox.service" Enter
Sleep 1800ms
Type "podman ps --format '{{.Names}}  {{.Status}}' | sort" Enter
Sleep 2500ms
Type "# models served locally by llama-swap (ROCm / gfx1201):" Sleep 400ms Enter
Type "curl -s localhost:8080/v1/models | jq -r '.data[].id'" Enter
Sleep 2200ms
Type "./download-model.sh list" Enter
Sleep 3200ms
Type "# AI-driven security lab, network-egress-pinned:" Sleep 400ms Enter
Type "podman exec kali-sandbox bash -lc 'id -un; hostname; nmap --version | head -1'" Enter
Sleep 2600ms
