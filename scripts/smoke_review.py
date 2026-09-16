"""End-to-end: Maia plays a beginner-level game, the full pipeline reviews it."""
import asyncio, random, time, chess, chess.pgn
from app.engines import Stockfish
from app.maia import Maia
from app.review import review_game

PLAYER_ELO = 1000  # ~ chess.com 550-600 rapid on the Lichess scale

async def main():
    random.seed(7)
    maia = Maia()
    sf = Stockfish(depth=14)
    await sf.start()

    # Generate a genuine beginner game by letting Maia play both sides.
    board = chess.Board()
    while not board.is_game_over() and board.fullmove_number <= 40:
        mv = maia.play_sync(board, PLAYER_ELO, temperature=1.0)
        if mv is None: break
        board.push(mv)
    moves = list(board.move_stack)
    print(f"generated a {len(moves)}-ply game at Maia Elo {PLAYER_ELO}, result {board.result()}")
    print("PGN:", str(chess.pgn.Game.from_board(board).mainline_moves())[:240], "...\n")

    t0 = time.time()
    rep = await review_game(moves, sf, maia=maia, player_elo=PLAYER_ELO, hero=chess.WHITE)
    print(f"reviewed {len(moves)} plies in {time.time()-t0:.1f}s\n")

    print(f"accuracy  white={rep.accuracy_white:.1f}%  black={rep.accuracy_black:.1f}%")
    print(f"acpl      white={rep.acpl_white:.0f}      black={rep.acpl_black:.0f}")
    print(f"errors (white): {len(rep.errors())}\n")

    print("=" * 78)
    print("TOP TEACHING MOMENTS  (ranked by severity x how common at your level)")
    print("=" * 78)
    for m in rep.lessons(limit=3):
        n = m.ply // 2 + 1
        print(f"\nMove {n}. {m.san}   [{m.judgment.value.upper()}]  "
              f"-{m.win_lost:.0f}% winning chances   ({m.phase})")
        if m.human_probability is not None:
            common = "COMMON TRAP" if m.is_systematic else "one-off slip"
            print(f"   at Elo {PLAYER_ELO}: {m.human_probability*100:.0f}% of players play this "
                  f"(rank #{m.human_rank}) -> {common}")
        if m.best_move_san:
            if m.best_is_findable:
                print(f"   better: {m.best_move_san}  "
                      f"({(m.best_human_probability or 0)*100:.0f}% of your peers find it)"
                      f"  line: {' '.join(m.best_pv_san[:4])}")
            else:
                print(f"   engine wants {m.best_move_san}, but only "
                      f"{(m.best_human_probability or 0)*100:.1f}% of players at your level "
                      f"find it -> teach the pattern, not the move")
        for f in m.findings[:3]:
            print(f"   * {f.phrase}")
        if m.refutation_san:
            print(f"   punished by: {' '.join(m.refutation_san[:4])}")

    await sf.close()

asyncio.run(main())
