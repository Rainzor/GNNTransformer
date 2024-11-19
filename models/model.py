import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import GCNConv, GraphConv, GATConv, SAGEConv
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader
from torch_geometric.utils import to_dense_batch

import math

from collections import OrderedDict

import numpy as np


class PositionalEncoder(nn.Module):
    def __init__(self, L):
        """
        Initializes the PositionalEncoder module.

        Parameters:
        - L (int): Number of frequency components in the encoding.
        """
        super(PositionalEncoder, self).__init__()
        self.L = L

        # Precompute the frequency terms (2^k * pi)
        frequencies = torch.tensor([1 << i for i in range(L)], dtype=torch.float32) * math.pi
        self.register_buffer('frequencies', frequencies)  # Shape: (L,)

    def forward(self, p):
        """
        Computes the positional encoding gamma(p) for a given input p in the range [-1, 1].

        Parameters:
        - p (torch.Tensor): Input tensor of shape (batch_size, dim) with values in the range [-1, 1].

        Returns:
        - torch.Tensor: Positional encoding of shape (batch_size, dim * L).
        """
        # Ensure p is on the same device as frequencies
        # (Not needed since frequencies will be moved with the model)

        # Ensure p has the correct shape to broadcast with frequencies
        p = p.unsqueeze(-1)  # Shape: (batch_size, dim, 1)
        
        # Apply sin transformation only
        sin_encodings = torch.sin(p * self.frequencies)  # Shape: (batch_size, dim, L) 

        return sin_encodings.view(p.size(0), -1)  # Shape: (batch_size, dim * L)


# GCN Module with Residual Connections
class GCN(nn.Module):
    def __init__(self, num_features, hidden_channels):
        super(GCN, self).__init__()
        torch.manual_seed(12345)
        # GCN Layers
        self.conv1 = GCNConv(num_features, hidden_channels)
        self.conv2 = GCNConv(hidden_channels, hidden_channels)
        self.conv3 = GCNConv(hidden_channels, hidden_channels)
        self.skip1 = nn.Linear(num_features, hidden_channels) if num_features != hidden_channels else nn.Identity()
        self.skip2 = nn.Identity()
        self.skip3 = nn.Identity()

    def forward(self, x, edge_index):
        # First Layer with Skip Connection
        residual = self.skip1(x)
        x = self.conv1(x, edge_index)
        x = F.relu(x + residual)
        x = F.dropout(x, p=0.3, training=self.training)
        
        # Second Layer with Skip Connection
        residual = self.skip2(x)
        x = self.conv2(x, edge_index)
        x = F.relu(x + residual)
        x = F.dropout(x, p=0.3, training=self.training)
        
        # Third Layer with Skip Connection
        residual = self.skip3(x)
        x = self.conv3(x, edge_index)
        x = F.relu(x + residual)
        x = F.dropout(x, p=0.3, training=self.training)
        
        return x

    def reset_parameters(self):
        self.conv1.reset_parameters()
        self.conv2.reset_parameters()
        self.conv3.reset_parameters()
        if isinstance(self.skip1, nn.Linear):
            self.skip1.reset_parameters()

 
class GELU(nn.Module):
    def __init__(self):
        super(GELU, self).__init__()

    def forward(self, x):
        return 0.5 * x * (1 + F.tanh(np.sqrt(2 / np.pi) * (x + 0.044715 * torch.pow(x,3))))
    
# Residual Attention Block
class ResidualAttentionBlock(nn.Module):
    def __init__(self, d_model: int, n_head: int):
        super(ResidualAttentionBlock, self).__init__()
        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_1 = nn.LayerNorm(d_model)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, d_model * 4)),
            ("gelu", GELU()),
            ("c_proj", nn.Linear(d_model * 4, d_model))
        ]))
        self.ln_2 = nn.LayerNorm(d_model)

    def attention(self, x: torch.Tensor, key_padding_mask: torch.Tensor = None):
        return self.attn(x, x, x, key_padding_mask=key_padding_mask)[0]

    def forward(self, x: torch.Tensor, key_padding_mask: torch.Tensor = None):
        x = x + self.attention(self.ln_1(x), key_padding_mask=key_padding_mask)
        x = x + self.mlp(self.ln_2(x))
        return x

# Transformer Module
class Transformer(nn.Module):
    def __init__(self, width: int, layers: int, heads: int):
        super(Transformer, self).__init__()
        self.width = width
        self.layers = layers
        self.resblocks = nn.ModuleList([
            ResidualAttentionBlock(width, heads) for _ in range(layers)
        ])

    def forward(self, x: torch.Tensor, key_padding_mask: torch.Tensor = None):
        for block in self.resblocks:
            x = block(x, key_padding_mask=key_padding_mask)
        return x


# GCNTransformer class, mimicking Vision Transformer structure
class GCNTransformer(nn.Module):
    def __init__(
        self,
        num_features: int,
        transformer_width: int,
        transformer_layers: int,
        transformer_heads: int,
        hidden_dim: int,
        output_dim: int,
        embedding_dim: int,  # Number of frequency components for PositionalEncoder
        pos_dim: int,  # Dimension of positional features
        dropout: float = 0.1,
    ):
        super(GCNTransformer, self).__init__()
        self.num_features = num_features
        self.output_dim = output_dim

        # Positional Encoder
        self.positional_encoder = PositionalEncoder(L=embedding_dim)

        # GCN module
        self.gcn = GCN(num_features + embedding_dim*pos_dim, embedding_dim)

        # [CLS] token as a learnable embedding
        self.cls_token = nn.Parameter(torch.zeros(1, 1, transformer_width))

        # Mapping GCN output to Transformer input dimension
        self.gcn_to_transformer = nn.Linear(embedding_dim*(pos_dim+1), transformer_width)

        # Layer normalization before Transformer
        self.ln_pre = nn.LayerNorm(transformer_width)

        # If positional encoding increases the dimension, adjust the transformer width accordingly
        self.transformer = Transformer(width=transformer_width, layers=transformer_layers, heads=transformer_heads)

        # Layer normalization after Transformer
        self.ln_post = nn.LayerNorm(transformer_width)

        self.decoder = nn.Sequential(
            nn.Linear(transformer_width, hidden_dim),
            nn.Sigmoid(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Sigmoid(),
            nn.Linear(hidden_dim, output_dim)
        )

        # Initialize weights
        self._init_weights()

    def _init_weights(self):
        nn.init.trunc_normal_(self.cls_token, std=0.02)
        # Initialize GCN to Transformer mapping
        nn.init.trunc_normal_(self.gcn_to_transformer.weight, std=0.02)
        if self.gcn_to_transformer.bias is not None:
            nn.init.zeros_(self.gcn_to_transformer.bias)
        # Positional Encoder weightare fixed (sinusoidal), no initialization needed

    def forward(self, x, edge_index, p, batch):
        """
        x: [total_num_nodes, num_features]
        edge_index: [2, num_edges]
        p: [total_num_nodes, pos_dim], positional features for each node in the range [-1, 1]
        batch: [total_num_nodes], indicating the graph index each node belongs to
        """

        pos_enc = self.positional_encoder(p)  # [total_num_nodes, embedding_dim * pos_dim]

        x = torch.cat([x, pos_enc], dim=-1)  # [total_num_nodes, num_features + embedding_dim * pos_dim]
        # Extract node features using GCN
        x = self.gcn(x, edge_index)  # [total_num_nodes, embedding_dim]

        x = torch.cat([x, pos_enc], dim=-1) # [total_num_nodes, embedding_dim*(pos_dim+1)]

        # Map to Transformer input dimension
        x = self.gcn_to_transformer(x)  # [total_num_nodes, transformer_width]

        x_dense, mask = to_dense_batch(x, batch)  # x_dense: [batch_size, max_num_nodes, transformer_width]; mask: [batch_size, max_num_nodes]

        batch_size, max_num_nodes, _ = x_dense.size()
        cls_tokens = self.cls_token.expand(batch_size, -1, -1)  # [batch_size, 1, transformer_width]
        x_dense = torch.cat([cls_tokens, x_dense], dim=1)  # [batch_size, 1 + max_num_nodes, transformer_width]

        # Add True for [CLS] tokens
        cls_mask = torch.ones(batch_size, 1, dtype=torch.bool, device=x.device)
        attention_mask = torch.cat([cls_mask, mask], dim=1)  # [batch_size, 1 + max_num_nodes]

        # Permute to match Transformer input shape (sequence_length, batch_size, embedding_dim)
        x_dense = x_dense.permute(1, 0, 2)  # [1 + max_num_nodes, batch_size, transformer_width]

        # Forward pass through Transformer
        x_transformed = self.transformer(x_dense, key_padding_mask=~attention_mask)  # [1 + max_num_nodes, batch_size, transformer_width]

        # Permute back to (batch_size, sequence_length, embedding_dim)
        x_transformed = x_transformed.permute(1, 0, 2)  # [batch_size, 1 + max_num_nodes, transformer_width]

        # Extract the [CLS] token's features
        cls_features = self.ln_post(x_transformed[:, 0, :])  # [batch_size, transformer_width]

        # Pass through the decoder
        out = self.decoder(cls_features)  # [batch_size, output_dim]

        return out  # [batch_size, output_dim]

    def vmf_param(self, x, edge_index, p, batch):
        """
        计算模型输出的von Mises-Fisher (vMF)分布参数。
        
        参数：
        - x (torch.Tensor): 节点特征矩阵，形状为 [total_num_nodes, num_features]
        - edge_index (torch.Tensor): 边索引，形状为 [2, num_edges]
        - p (torch.Tensor): 位置特征，形状为 [total_num_nodes, pos_dim]，范围为 [-1, 1]
        - batch (torch.Tensor): 批量向量，指示每个节点所属的图，形状为 [total_num_nodes]
        
        返回：
        - weights (torch.Tensor): [batch_size, num_vmf]
        - mus (torch.Tensor): [batch_size, num_vmf, 3]
        - kappas (torch.Tensor): [batch_size, num_vmf]
        """
        with torch.no_grad():
            # 前向传播，获取模型输出
            out = self(x, edge_index, p, batch)  # [batch_size, output_dim]
            
            # 确定批量大小和vMF组件的数量
            batch_size = out.size(0)  # [batch_size]
            output_dim = out.size(1)  # output_dim = num_vmf * 4
            assert output_dim % 4 == 0, "Output dimension must be divisible by 4 for vMF parameters"
            num_vmf = output_dim // 4  # 每个图的vMF组件数量
            
            # 重塑输出为 [batch_size, num_vmf, 4]
            out = out.view(batch_size, num_vmf, 4)  # [batch_size, num_vmf, 4]
            
            # 计算vMF参数
            weights = F.softmax(out[:, :, 0], dim=-1)  # [batch_size, num_vmf]
            kappas = torch.exp(out[:, :, 1])           # [batch_size, num_vmf]
            theta = torch.sigmoid(out[:, :, 2]) * math.pi  # [batch_size, num_vmf]
            phi = torch.sigmoid(out[:, :, 3]) * math.pi * 2  # [batch_size, num_vmf]
            
            # 将球面坐标（theta, phi）转换为笛卡尔坐标（mu向量）
            cos_theta = torch.cos(theta)  # [batch_size, num_vmf]
            sin_theta = torch.sin(theta)  # [batch_size, num_vmf]
            cos_phi = torch.cos(phi)      # [batch_size, num_vmf]
            sin_phi = torch.sin(phi)      # [batch_size, num_vmf]
            
            # 组合mu向量： [batch_size, num_vmf, 3]
            mus = torch.stack(
                (sin_theta * cos_phi, sin_theta * sin_phi, cos_theta),
                dim=-1
            )  # [batch_size, num_vmf, 3]
            
            return weights, mus, kappas
