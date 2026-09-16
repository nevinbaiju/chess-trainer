"""Mate detection, checked against a game whose mate distances are known.

Morphy's Opera Game finishes 16.Qb8+ Nxb8 17.Rd8#. So at White's move 17 it is
mate in 1, at move 16 mate in 2, and at move 15 (15.Bxd7+ Nxd7 16.Qb8+ Nxb8
17.Rd8#) mate in 3.
"""
import asyncio, io, time, chess, chess.pgn
from app.engines import Stockfish

OPERA = """1. e4 e5 2. Nf3 d6 3. d4 Bg4 4. dxe5 Bxf3 5. Qxf3 dxe5 6. Bc4 Nf6
7. Qb3 Qe7 8. Nc3 c6 9. Bg5 b5 10. Nxb5 cxb5 11. Bxb5+ Nbd7 12. O-O-O Rd8
13. Rxd7 Rxd7 14. Rd1 Qe6 15. Bxd7+ Nxd7 16. Qb8+ Nxb8 17. Rd8# 1-0"""

async def main():
    sf = Stockfish(); await sf.start()
    game = chess.pgn.read_game(io.StringIO(OPERA))
    moves = list(game.mainline_moves())

    boards = [chess.Board()]
    b = chess.Board()
    for m in moves:
        b.push(m); boards.append(b.copy())

    print(f"{'ply':>4} {'to move':>8} {'mate in':>8}  {'ms':>5}")
    for ply in (24, 26, 28, 30, 32):
        pos = boards[ply]
        t = time.time()
        n = await sf.find_mate(pos, max_moves=3)
        ms = (time.time()-t)*1000
        turn = "white" if pos.turn == chess.WHITE else "black"
        print(f"{ply:4d} {turn:>8} {str(n):>8}  {ms:5.0f}")

    print("\n-- the cap --")
    deep = boards[24]
    for cap in (1, 2, 3, 5):
        print(f"  max_moves={cap} -> {await sf.find_mate(deep, max_moves=cap)}")

    print("\n-- no mate available --")
    for label, fen in [
        ("start position", chess.STARTING_FEN),
        ("quiet middlegame", "r2q1rk1/1b2bppp/p2ppn2/1p6/3NPP2/2N1B3/PPPQ2PP/2KR1B1R w - - 0 13"),
    ]:
        print(f"  {label:18s} -> {await sf.find_mate(chess.Board(fen), max_moves=3)}")

    print("\n-- mate against the side to move is not reported as an opportunity --")
    losing = boards[31]   # black to move, getting mated
    print(f"  black to move facing mate -> {await sf.find_mate(losing, max_moves=3)}")

    print("\n-- cost when called on every move --")
    t = time.time()
    for ply in range(0, 20):
        await sf.find_mate(boards[ply], max_moves=3)
    print(f"  20 lookups in {(time.time()-t)*1000:.0f}ms")
    await sf.close()

asyncio.run(main())
