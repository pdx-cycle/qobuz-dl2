from .qopy import Client as Client


def main():
    from .cli import main as cli_main

    return cli_main()
