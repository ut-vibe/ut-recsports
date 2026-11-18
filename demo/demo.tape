# utcourts demo: check what's open, book a court, see it in your reservations.
# Render: vhs demo/demo.tape   (books a real court: answer is the `Type "y"` below)

Output demo/demo.mp4
Output demo/demo.gif

Set Shell "bash"
Set Theme "Catppuccin Mocha"
Set FontSize 18
Set Width 1280
Set Height 760
Set Padding 28
Set WindowBar Colorful
Set TypingSpeed 45ms
Set WaitTimeout 90s
Set CursorBlink false

Hide
Type 'export PATH="$HOME/.local/bin:$PATH"; unset NO_COLOR; cd /tmp; clear'
Enter
Sleep 500ms
Show

Sleep 800ms
Type "utcourts tennis today --after 7pm"
Sleep 700ms
Enter
Wait
Sleep 4s

Hide
Ctrl+L
Sleep 300ms
Show

Type "utcourts book tennis today 8pm"
Sleep 700ms
Enter
Wait /Which one/
Sleep 2500ms
Type "1"
Sleep 700ms
Enter
Wait /\[y.N\]/
Sleep 2s
Type "y"
Sleep 700ms
Enter
Wait
Sleep 4s

Hide
Ctrl+L
Sleep 300ms
Show

Type "utcourts mine"
Sleep 700ms
Enter
Wait
Sleep 5s
