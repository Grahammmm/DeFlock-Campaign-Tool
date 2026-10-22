"""Fresh-process pre-import network guard for the intake security regression."""
from pathlib import Path
import socket
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    def denied(*args, **kwargs):
        raise RuntimeError("network attempted by offline intake import/default policy")

    with mock.patch.object(socket.socket, "connect", side_effect=denied) as connect, \
            mock.patch.object(socket, "create_connection", side_effect=denied) as create:
        from campaign_tool.records.intake import eml_export, imap_intake
        config = {"ssl": False, "host": "synthetic.invalid", "port": 143,
                  "timeout": 1, "username": "synthetic", "password": "synthetic"}
        try:
            imap_intake.default_client_factory(config)
        except imap_intake.IntakeError as error:
            if error.args != ("mail_config_plaintext_refused",):
                raise RuntimeError("wrong policy failure") from None
        else:
            raise RuntimeError("plaintext default was accepted")
        if connect.called or create.called or not callable(eml_export.attachment_forms):
            raise RuntimeError("offline boundary violated")
    print("PASS: guarded pre-import and plaintext default; zero network attempts")


if __name__ == "__main__":
    main()
