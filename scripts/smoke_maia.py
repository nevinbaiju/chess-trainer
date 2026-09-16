"""Phase 0 exit criterion: Maia-3 loads, reads positions, and plays a legal game."""
import time, chess
from app.maia import Maia

t0 = time.time()
maia = Maia()
print(f"model loaded in {time.time()-t0:.1f}s ({maia.model_name})\n")

board = chess.Board()
for elo in (600, 1000, 1500, 2000, 2600):
    t = time.time()
    read = maia.read_sync(board, elo)
    ms = (time.time() - t) * 1000
    top = ", ".join(f"{board.san(o.move)} {o.percent:.0f}%" for o in read.odds[:5])
    print(f"Elo {elo:4d}  ({ms:5.1f}ms)  {top}")

print("\n--- after 1.e4 e5 2.Nf3 : who defends? ---")
b = chess.Board()
for san in ("e4", "e5", "Nf3"):
    b.push(b.parse_san(san))
for elo in (600, 1000, 1800):
    read = maia.read_sync(b, elo)
    top = ", ".join(f"{b.san(o.move)} {o.percent:.0f}%" for o in read.odds[:4])
    print(f"Elo {elo:4d}  {top}")
    print(f"          human W/D/L = {read.win:.2f}/{read.draw:.2f}/{read.loss:.2f}")

print("\n--- probability mass sums to 1? ---")
read = maia.read_sync(chess.Board(), 1000)
print(f"legal moves={len(read.odds)}  sum(policy)={sum(o.policy for o in read.odds):.6f}")

print("\n--- self-play, Elo 1000, temperature 1.0 ---")
b = chess.Board()
t0 = time.time(); plies = 0
while not b.is_game_over() and plies < 80:
    mv = maia.play_sync(b, 1000, temperature=1.0)
    if mv is None or mv not in b.legal_moves:
        print(f"!! ILLEGAL/None move at ply {plies}: {mv}"); break
    b.push(mv); plies += 1
print(f"{plies} plies in {time.time()-t0:.1f}s ({(time.time()-t0)/max(plies,1)*1000:.0f}ms/move)")
print("result:", b.result(), "| over:", b.is_game_over())
import chess.pgn
g = chess.pgn.Game.from_board(b)
print("game:", str(g.mainline_moves())[:300])
