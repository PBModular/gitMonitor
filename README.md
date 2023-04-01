# gitMonitor Module

This PBModular module can monitor a GitHub repository and send a message to a Telegram group when a new commit is made. It uses Pyrogram and aiohttp.

## Usage

To use this module, add your GitHub token into `main.py`, then drop gitMonitor_module folder to PBModular/modules. Once the module is added to the bot, users can use the following commands:

- `/git_start` - start the configuration process
- `/git_src <url>` - set the URL of the GitHub repository to monitor
- `/git_reset` - stop monitoring the repository

After the `git_src` command is used, the module will start monitoring the repository and send a message to the group chat whenever a new commit is made.
