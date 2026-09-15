on run argv
    set launcherPath to item 1 of argv
    tell application "Terminal"
        activate
        do script "exec " & quoted form of launcherPath
    end tell
end run
