"""Phase 0 exit criterion: Stockfish analyses a real PGN end to end."""
import asyncio, io, time, chess, chess.pgn
from app.engines import Stockfish
from app.eval import Eval, classify, eval_win_percent, accuracy_pct, game_accuracy, move_win_percents
from app.phases import divide_game

OPERA = """1. e4 e5 2. Nf3 d6 3. d4 Bg4 4. dxe5 Bxf3 5. Qxf3 dxe5 6. Bc4 Nf6
7. Qb3 Qe7 8. Nc3 c6 9. Bg5 b5 10. Nxb5 cxb5 11. Bxb5+ Nbd7 12. O-O-O Rd8
13. Rxd7 Rxd7 14. Rd1 Qe6 15. Bxd7+ Nxd7 16. Qb8+ Nxb8 17. Rd8# 1-0"""

async def main():
    sf = Stockfish(depth=14)
    await sf.start()
    print("engine:", sf.id.get("name"))

    game = chess.pgn.read_game(io.StringIO(OPERA))
    moves = list(game.mainline_moves())

    t0 = time.time()
    board = chess.Board()
    evals, positions = [], [board.copy()]
    for mv in moves:
        board.push(mv)
        evals.append(await sf.evaluate(board))
        positions.append(board.copy())
    elapsed = time.time() - t0
    print(f"analysed {len(moves)} plies at depth 14 in {elapsed:.1f}s "
          f"({elapsed/len(moves)*1000:.0f}ms/ply)")

    wp = move_win_percents(evals)
    div = divide_game(chess.Board(), moves)
    print(f"phases: {div}")
    print(f"accuracy  white={game_accuracy(wp, True):.1f}%  black={game_accuracy(wp, False):.1f}%")

    print("\nply  move     eval        win%   verdict     phase")
    b = chess.Board()
    prev = Eval(cp=15)
    for i, mv in enumerate(moves):
        san = b.san(mv)
        white = b.turn == chess.WHITE
        cur = evals[i]
        j = classify(prev, cur, white)
        show = f"M{cur.mate}" if cur.is_mate else f"{cur.cp/100:+.2f}"
        if j or i >= len(moves) - 3:
            print(f"{i:3d}  {san:8s} {show:>8s}  {eval_win_percent(cur):5.1f}  "
                  f"{(j.value if j else ''):11s} {div.phase_at(i)}")
        prev = cur
        b.push(mv)

    print("\ncache entries:", len(sf._cache))
    await sf.close()

asyncio.run(main())
