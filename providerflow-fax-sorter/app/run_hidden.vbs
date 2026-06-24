' Launches the scheduled fax-sorter run with no visible window.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = appDir
sh.Run "cmd /c """ & appDir & "\run_scheduled_internal.bat""", 0, False
