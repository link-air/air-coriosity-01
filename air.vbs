' air - open dashboard without console window (double-click me)
' Stop: use the End button on the dashboard page (she finishes the current round first).
'
' Paths are derived from this script's own folder, so nothing here is tied to a
' particular drive letter. Move the project anywhere and it still works.
Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = here

py = here & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then py = "pythonw"    ' no venv yet: fall back to system pythonw
dash = here & "\dashboard.py"

ws.Run """" & py & """ """ & dash & """", 0, False
WScript.Sleep 1500
ws.Run "http://127.0.0.1:9878"
