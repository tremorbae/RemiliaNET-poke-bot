# RemiliaNET poke bot

Pokes someone on RemiliaNET every 1–5 minutes, around the clock. Friends come first, then everyone else. After poking someone who isn't a friend, it sends them a friend request, and it never asks again if they decline.

## Start

```
./install_browser.sh
```

The first time, a Chrome window opens. Log in and tick **Remember me**, and the bot starts.

## Stop

```
./install_browser.sh --uninstall
```

## See what it's doing

```
tail -f ~/Library/Logs/remilianet-poke-browser.log
```

## Good to know

- It needs Google Chrome installed, and it only runs while your Mac is awake.
- If it signs out, you'll get a notification. Run `python3 poke_browser.py --login` to sign back in.
- After editing `poke_browser.py`, run `./install_browser.sh` again.
