"""Does the Elo dial actually change how strongly Maia plays?

The whole adaptive-difficulty design rests on this. If Maia at 600 plays about
as well as Maia at 1200, the dial is a placebo and the app needs a different
weakening mechanism entirely.

Method: round-robin, each conditioning level against a fixed 1200 reference,
sampling at temperature 1.0 (the same setting the app plays at).
"""
import itertools, random, sys, time
import chess
from app.maia import Maia

LEVELS = [600, 800, 1000, 1200]
GAMES = int(sys.argv[1]) if len(sys.argv) > 1 else 12
MAX_PLIES = 160

def play(maia, white_elo, black_elo, seed):
    random.seed(seed)
    board = chess.Board()
    while not board.is_game_over(claim_draw=True) and len(board.move_stack) < MAX_PLIES:
        elo = white_elo if board.turn == chess.WHITE else black_elo
        opp = black_elo if board.turn == chess.WHITE else white_elo
        mv = maia.play_sync(board, elo, oppo_elo=opp, temperature=1.0)
        if mv is None or mv not in board.legal_moves:
            break
        board.push(mv)
    out = board.outcome(claim_draw=True)
    if out is None or out.winner is None:
        return 0.5
    return 1.0 if out.winner == chess.WHITE else 0.0

maia = Maia()
print(f"{GAMES} games per pairing, temperature 1.0, max {MAX_PLIES} plies\n")

REF = 1200
print(f"each level vs a fixed {REF} reference:")
print(f"{'level':>6} {'score':>7} {'W-D-L':>10} {'implied gap':>12}")
import math
for level in LEVELS:
    pts = 0.0; w=d=l=0
    for g in range(GAMES):
        # alternate colours so first-move advantage cancels
        if g % 2 == 0:
            s = play(maia, level, REF, 1000+g)
        else:
            s = 1.0 - play(maia, REF, level, 1000+g)
        pts += s
        if s == 1.0: w+=1
        elif s == 0.5: d+=1
        else: l+=1
    score = pts/GAMES
    gap = 400*math.log10(max(score,0.01)/max(1-score,0.01))
    print(f"{level:>6} {score:>6.0%} {w:>4}-{d}-{l:<4} {gap:>+11.0f}")
