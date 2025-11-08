cp launch.plist ~/Library/LaunchAgents/com.user.syncscript.plist
launchctl load ~/Library/LaunchAgents/com.user.syncscript.plist
launchctl list | grep syncscript

# launchctl unload ~/Library/LaunchAgents/com.user.syncscript.plist
