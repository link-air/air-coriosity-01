"""air-curiosity-01 的轻量象棋对手引擎（依赖 python-chess）。

设计意图：
- 完全离线，不依赖 Stockfish 等二进制
- 难度可调（depth + 随机扰动），让 air 先易后难
- 合法走法由 python-chess 保证

评估：子力价值 + 简化的位置表（piece-square table）
搜索：negamax + alpha-beta + 吃子优先排序
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜常量 —— 子力基础价值 PIECE_VALUE + 位置表（_PAWN_PST 等）
#   段 2｜评估 —— evaluate()：静态评估（子力 + 位置 + 将死/无子判断）
#   段 3｜搜索 —— _order_moves 吃子优先 + _negamax（alpha-beta 剪枝）
#   段 4｜选步入口 —— choose_move()：难度可调（depth + blunder_chance）
# =====================================================================

import chess
import random

# 子力基础价值
PIECE_VALUE = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}

# 简化的兵位置表（中心更好），其他子用对称简化表
_PAWN_PST = [
     0,  0,  0,  0,  0,  0,  0,  0,
     5, 10, 10,-20,-20, 10, 10,  5,
     5, -5,-10,  0,  0,-10, -5,  5,
     0,  0,  0, 20, 20,  0,  0,  0,
     5,  5, 10, 25, 25, 10,  5,  5,
    10, 10, 20, 30, 30, 20, 10, 10,
    50, 50, 50, 50, 50, 50, 50, 50,
     0,  0,  0,  0,  0,  0,  0,  0,
]
_KNIGHT_PST = [
    -50,-40,-30,-30,-30,-30,-40,-50,
    -40,-20,  0,  5,  5,  0,-20,-40,
    -30,  5, 10, 15, 15, 10,  5,-30,
    -30,  0, 15, 20, 20, 15,  0,-30,
    -30,  5, 15, 20, 20, 15,  5,-30,
    -30,  0, 10, 15, 15, 10,  0,-30,
    -40,-20,  0,  0,  0,  0,-20,-40,
    -50,-40,-30,-30,-30,-30,-40,-50,
]
_GENERIC_PST = [0] * 64  # 车/象/后/王用平坦估值，避免复杂表


def _pst_value(piece_type, square, color):
    """返回位置表加分（白方视角，自动适配颜色）。"""
    if piece_type == chess.PAWN:
        table = _PAWN_PST
    elif piece_type == chess.KNIGHT:
        table = _KNIGHT_PST
    else:
        table = _GENERIC_PST
    idx = square if color == chess.WHITE else chess.square_mirror(square)
    return table[idx]


def evaluate(board: chess.Board) -> int:
    """静态评估，正分对白方有利。"""
    if board.is_checkmate():
        # 轮到谁走谁被将死 → 对方赢
        return -100000 if board.turn == chess.WHITE else 100000
    if board.is_stalemate() or board.is_insufficient_material():
        return 0

    score = 0
    for sq, piece in board.piece_map().items():
        val = PIECE_VALUE[piece.piece_type] + _pst_value(piece.piece_type, sq, piece.color)
        score += val if piece.color == chess.WHITE else -val
    return score


def _order_moves(board: chess.Board):
    """吃子优先，提升 alpha-beta 剪枝效率。"""
    moves = list(board.legal_moves)
    def key(m):
        if board.is_capture(m):
            victim = board.piece_at(m.to_square)
            return 10000 + (PIECE_VALUE[victim.piece_type] if victim else 100)
        return 0
    moves.sort(key=key, reverse=True)
    return moves


def _negamax(board: chess.Board, depth: int, alpha: int, beta: int, color: int):
    """color: +1 白方视角, -1 黑方视角。返回当前局面对 color 方的评分。"""
    if board.is_game_over():
        val = evaluate(board)
        return val * color
    if depth == 0:
        return evaluate(board) * color

    best = -10**9
    for move in _order_moves(board):
        board.push(move)
        val = -_negamax(board, depth - 1, -beta, -alpha, -color)
        board.pop()
        if val > best:
            best = val
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best


def choose_move(board: chess.Board, depth: int = 3, blunder_chance: float = 0.0) -> chess.Move:
    """为当前行棋方选一步。

    depth: 搜索深度（越大越强）
    blunder_chance: 0~1，随机走废棋的概率（用于低难度放海）
    """
    if board.is_game_over():
        return None
    legal = list(board.legal_moves)
    if not legal:
        return None

    color = 1 if board.turn == chess.WHITE else -1

    if blunder_chance > 0 and random.random() < blunder_chance:
        return random.choice(legal)

    best_move = None
    best_val = -10**9
    alpha = -10**9
    beta = 10**9
    for move in _order_moves(board):
        board.push(move)
        val = -_negamax(board, depth - 1, -beta, -alpha, -color)
        board.pop()
        if val > best_val:
            best_val = val
            best_move = move
        if val > alpha:
            alpha = val
    return best_move


if __name__ == "__main__":
    b = chess.Board()
    print("startpos legal moves:", b.legal_moves.count())
    # 引擎（白）走一步
    m1 = choose_move(b, depth=3)
    print("engine plays:", m1)
    b.push(m1)
    # air（黑）走 e5 后引擎回应
    b.push(chess.Move.from_uci("e7e5"))
    m2 = choose_move(b, depth=3)
    print("engine replies after e5:", m2)
    print("OK")
