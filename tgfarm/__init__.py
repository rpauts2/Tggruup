"""tgfarm — autonomous Telegram channel farm.

Pipeline: S1 sources -> S2 trends -> S3 router -> S4 generation
          -> S5 quality -> S6 visual -> S7 publish -> S8 analytics
          -> S9 crossposting
"""

__version__ = "1.0.0"