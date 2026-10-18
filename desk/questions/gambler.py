"""Gambler: directional bets. Asks only where price is going."""
from desk.questions.base import OTHER_MEANS, Battery, Choice

BATTERY = Battery(
    bot="gambler",
    questions=(
        Choice(
            id="g_dir_5m",
            text="Over the next 5 minutes, which way will the mid move by more than 2 bps?",
            options=("up", "down", "flat", "other"),
            resolver="direction_2bps",
            horizon_s=300,
            describe={"up": "Mid ends more than 2 bps above the current mid.",
                      "down": "Mid ends more than 2 bps below the current mid.",
                      "flat": "Mid ends within 2 bps of the current mid.",
                      "other": OTHER_MEANS},
        ),
        Choice(
            id="g_dir_30m",
            text="Over the next 30 minutes, which way will the mid move by more than 2 bps?",
            options=("up", "down", "flat", "other"),
            resolver="direction_2bps",
            horizon_s=1800,
            describe={"up": "Mid ends more than 2 bps above the current mid.",
                      "down": "Mid ends more than 2 bps below the current mid.",
                      "flat": "Mid ends within 2 bps of the current mid.",
                      "other": OTHER_MEANS},
        ),
        Choice(
            id="g_jump_15m",
            text="Will the mid move more than 25 bps from now in either direction within 15 minutes?",
            options=("yes", "no", "other"),
            resolver="jump_25bps",
            horizon_s=900,
            describe={"yes": "At some point in the window the mid is more than 25 bps away from the current mid.",
                      "no": "The mid stays within 25 bps of the current mid for the whole window.",
                      "other": OTHER_MEANS},
        ),
    ),
)
