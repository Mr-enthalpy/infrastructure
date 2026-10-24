#!/bin/sh
# Root-owned login shell for members.
#
# sshd invokes this with "-c <ForceCommand>". It deliberately ignores that
# command and starts the fixed login wrapper, which reads SSH_ORIGINAL_COMMAND.
# Keeping this shell outside /home prevents a member from influencing shell
# startup files. PermitUserRC is also disabled in the member sshd match block.
exec /usr/local/bin/udc-resource-login
