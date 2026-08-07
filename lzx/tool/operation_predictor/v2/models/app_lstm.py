"""V2 LSTM 模型，用于应用级的多步（multi-horizon）预测。

本模块使用 LSTM 对历史应用序列进行编码，并结合当前打开的应用集合、
时间特征与用户分组嵌入，预测每个目标 horizon 上的下一步应用分布。
"""

from __future__ import annotations

import torch
from torch import nn


class AppLSTMV2(nn.Module):
    """使用 LSTM 编码历史应用并为多个预测时刻生成预测结果。

    注：添加了中文注释以便阅读与维护。

    参数说明：
    - num_apps: 应用词表大小（用于 one-hot 或 embedding 的维度基础）
    - num_user_groups: 用户分组数量（用于用户分组嵌入）
    - horizons: 要预测的未来时刻列表（例如 [3,5,10]）
    - app_embedding_dim: 应用 embedding 的维度
    - group_embedding_dim: 用户分组 embedding 的维度
    - hidden_dim: LSTM 隐状态维度（以及后续共享层输入维度的一部分）
    - opened_dim: 将 opened_apps（二进制/稀疏向量）编码为低维表示后的维度
    - dropout: 共享 MLP 中的 dropout 比例
    """

    def __init__(
        self,
        num_apps: int,
        num_user_groups: int,
        horizons: list[int],
        app_embedding_dim: int = 32,
        group_embedding_dim: int = 8,
        hidden_dim: int = 64,
        opened_dim: int = 32,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        # 基本属性
        self.num_apps = num_apps
        # 确保 horizon 是整数列表
        self.horizons = [int(horizon) for horizon in horizons]

        # 嵌入层：将历史应用 id 映射为稠密向量
        self.app_embedding = nn.Embedding(num_apps, app_embedding_dim)
        # 用户分组嵌入（可选的辅助特征）
        self.group_embedding = nn.Embedding(num_user_groups, group_embedding_dim)

        # LSTM 用于编码历史应用序列（batch_first=True，输入形状为 (B, T)经过 embedding 后为 (B, T, D)）
        self.lstm = nn.LSTM(app_embedding_dim, hidden_dim, batch_first=True)

        # opened_apps 的编码器：输入是一个长度为 num_apps 的稀疏/二进制向量，先用线性层降维
        # 注意：这种设计在 num_apps 很大时内存占用高，实际使用中可改为稀疏或基于索引的编码
        self.opened_encoder = nn.Sequential(
            nn.Linear(num_apps, opened_dim),
            nn.ReLU(),
        )

        # 特征融合维度：由 LSTM 隐向量、opened 编码、用户分组嵌入以及额外时间特征组成
        # 这里的 +3 对应 time_feature 的维度（假设 time_feature 为 3 维），若不同请同步调整
        feature_dim = hidden_dim + opened_dim + group_embedding_dim + 3

        # 共享的 MLP 层，用于将拼接的特征映射到预测头的输入空间
        self.shared = nn.Sequential(
            nn.Linear(feature_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
        )

        # 为每个 horizon 创建单独的线性预测头，输出为对所有应用的 logits
        self.heads = nn.ModuleDict({str(horizon): nn.Linear(hidden_dim, num_apps) for horizon in self.horizons})

    def forward(
        self,
        history_apps: torch.Tensor,
        opened_apps: torch.Tensor,
        time_feature: torch.Tensor,
        user_group: torch.Tensor,
    ) -> dict[int, torch.Tensor]:
        """前向传播。

        输入：
        - history_apps: LongTensor, 形状 (B, T)，历史应用 id 序列
        - opened_apps: FloatTensor, 形状 (B, num_apps)，当前被打开应用的二进制/分数向量
        - time_feature: FloatTensor, 形状 (B, 3)，示例：小时/星期/时段等时间相关特征
        - user_group: LongTensor, 形状 (B,) 或 (B,1)，用户分组 id

        返回：字典 map: horizon -> logits Tensor，logits 形状为 (B, num_apps)
        """
        # 1) 把历史应用 id 转为 embedding 并通过 LSTM 编码
        history_emb = self.app_embedding(history_apps)
        _, (hidden, _) = self.lstm(history_emb)
        # hidden 的形状为 (num_layers * num_directions, B, hidden_dim)，取最后一层
        history_repr = hidden[-1]

        # 2) 编码 opened_apps（从稀疏/高维向量降维）
        opened_repr = self.opened_encoder(opened_apps)

        # 3) 用户分组嵌入
        group_repr = self.group_embedding(user_group)

        # 4) 拼接所有特征：历史表示 + opened 表示 + 时间特征 + 分组表示
        features = torch.cat([history_repr, opened_repr, time_feature, group_repr], dim=1)

        # 5) 共享 MLP 并通过每个 horizon 的头生成 logits
        shared = self.shared(features)
        return {horizon: self.heads[str(horizon)](shared) for horizon in self.horizons}
