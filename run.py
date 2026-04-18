"""Container entrypoint for the trading-loop service."""
from fund.crew import main_loop

if __name__ == "__main__":
    main_loop()
