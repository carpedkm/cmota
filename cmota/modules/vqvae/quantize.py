import torch
import torch.nn as nn
import torch.nn.functional as F

from einops import rearrange

class VectorQuantizer(nn.Module):
    def __init__(self, num_tokens, codebook_dim, beta, normalized=False, contrast=False):
        super().__init__()
        self.codebook_dim = codebook_dim
        self.num_tokens = num_tokens
        self.beta = beta

        self.embedding = nn.Embedding(self.num_tokens, self.codebook_dim)

    def forward(self, z):
        # reshape z -> (batch, height, width, channel) and flatten
        z = rearrange(z, 'b c h w -> b h w c')
        z_flattened = z.reshape(-1, self.codebook_dim)

        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z
        d = z_flattened.pow(2).sum(dim=1, keepdim=True) + \
            self.embedding.weight.pow(2).sum(dim=1) - 2 * \
            torch.einsum('bd,nd->bn', z_flattened, self.embedding.weight) # 'n d -> d n'


        encoding_indices = torch.argmin(d, dim=1)
        z_q = self.embedding(encoding_indices).view(z.shape)
        encodings = F.one_hot(encoding_indices, self.num_tokens).type(z.dtype)
        avg_probs = torch.mean(encodings, dim=0)
        perplexity = torch.exp(-torch.sum(avg_probs * torch.log(avg_probs + 1e-10)))

        # compute loss for embedding
        loss = self.beta * F.mse_loss(z_q.detach(), z) + F.mse_loss(z_q, z.detach())

        # preserve gradients
        z_q = z + (z_q - z).detach()

        # reshape back to match original input shape
        #z_q, 'b h w c -> b c h w'
        z_q = rearrange(z_q, 'b h w c -> b c h w')
        return z_q, loss, (perplexity, encodings, encoding_indices)


class EmbeddingEMA(nn.Module):
    def __init__(self, num_tokens, codebook_dim, decay=0.99, eps=1e-5):
        super().__init__()
        self.decay = decay
        self.eps = eps        
        weight = torch.randn(num_tokens, codebook_dim)
        self.weight = nn.Parameter(weight, requires_grad = False)
        self.cluster_size = nn.Parameter(torch.zeros(num_tokens), requires_grad = False)
        self.embed_avg = nn.Parameter(weight.clone(), requires_grad = False)
        self.update = True

    def forward(self, embed_id):
        return F.embedding(embed_id, self.weight)

    def cluster_size_ema_update(self, new_cluster_size):
        self.cluster_size.data.mul_(self.decay).add_(new_cluster_size, alpha=1 - self.decay)

    def embed_avg_ema_update(self, new_embed_avg): 
        self.embed_avg.data.mul_(self.decay).add_(new_embed_avg, alpha=1 - self.decay)

    def weight_update(self, num_tokens):
        n = self.cluster_size.sum()
        smoothed_cluster_size = (
                (self.cluster_size + self.eps) / (n + num_tokens * self.eps) * n
            )
        #normalize embedding average with smoothed cluster size
        embed_normalized = self.embed_avg / smoothed_cluster_size.unsqueeze(1)
        self.weight.data.copy_(embed_normalized)   

class EMAVectorQuantizer(nn.Module):
    def __init__(self, num_tokens, codebook_dim, beta, decay=0.99, eps=1e-5):
        super().__init__()
        self.codebook_dim = codebook_dim
        self.num_tokens = num_tokens
        self.beta = beta
        self.embedding = EmbeddingEMA(self.num_tokens, self.codebook_dim, decay, eps)

    def forward(self, z):
        # reshape z -> (batch, height, width, channel) and flatten
        #z, 'b c h w -> b h w c'
        z = rearrange(z, 'b c h w -> b h w c')
        z_flattened = z.reshape(-1, self.codebook_dim)
        
        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z
        d = z_flattened.pow(2).sum(dim=1, keepdim=True) + \
            self.embedding.weight.pow(2).sum(dim=1) - 2 * \
            torch.einsum('bd,nd->bn', z_flattened, self.embedding.weight) # 'n d -> d n'


        encoding_indices = torch.argmin(d, dim=1)

        z_q = self.embedding(encoding_indices).view(z.shape)
        encodings = F.one_hot(encoding_indices, self.num_tokens).type(z.dtype)     
        avg_probs = torch.mean(encodings, dim=0)
        perplexity = torch.exp(-torch.sum(avg_probs * torch.log(avg_probs + 1e-10)))

        if self.training and self.embedding.update:
            #EMA cluster size
            encodings_sum = encodings.sum(0)            
            self.embedding.cluster_size_ema_update(encodings_sum)
            #EMA embedding average
            embed_sum = encodings.transpose(0,1) @ z_flattened            
            self.embedding.embed_avg_ema_update(embed_sum)
            #normalize embed_avg and update weight
            self.embedding.weight_update(self.num_tokens)

        # compute loss for embedding
        loss = self.beta * F.mse_loss(z_q.detach(), z) 

        # preserve gradients
        z_q = z + (z_q - z).detach()

        # reshape back to match original input shape
        #z_q, 'b h w c -> b c h w'
        z_q = rearrange(z_q, 'b h w c -> b c h w')
        return z_q, loss, (perplexity, encodings, encoding_indices)

class GumbelQuantizer(nn.Module):
    def __init__(self, num_tokens, codebook_dim, straight_through=True,
                 kl_weight=5e-4, temp_init=1.0):
        super().__init__()

        self.codebook_dim = codebook_dim
        self.num_tokens = num_tokens

        self.straight_through = straight_through
        self.temperature = temp_init
        self.kl_weight = kl_weight

        self.embedding = nn.Embedding(num_tokens, codebook_dim)

    def forward(self, z):
        # force hard = True when we are in eval mode, as we must quantize. actually, always true seems to work
        hard = self.straight_through if self.training else True
        temp = self.temperature 

        soft_one_hot = F.gumbel_softmax(z, tau=temp, dim=1, hard=hard)
        z_q = torch.einsum('b n h w, n d -> b d h w', soft_one_hot, self.embedding.weight)

        # + kl divergence to the prior loss
        qy = F.softmax(z, dim=1)
        loss = self.kl_weight * torch.sum(qy * torch.log(qy * self.num_tokens + 1e-10), dim=1).mean()

        
        encoding_indices = soft_one_hot.argmax(dim=1)
        encoding_indices = rearrange(encoding_indices, 'b h w -> (b h w)')
        encodings = F.one_hot(encoding_indices, self.num_tokens).type(z.dtype)
        avg_probs = torch.mean(encodings, dim=0)
        perplexity = torch.exp(-torch.sum(avg_probs * torch.log(avg_probs + 1e-10)))

        return z_q, loss, (perplexity, encodings, encoding_indices)

#Original Sonnet version of EMAVectorQuantizer
class SonnetEmbeddingEMA(nn.Module):
    def __init__(self, num_tokens, codebook_dim):
        super().__init__()
        weight = torch.randn(codebook_dim, num_tokens)
        self.register_buffer("weight", weight)
        self.register_buffer("cluster_size", torch.zeros(num_tokens))
        self.register_buffer("embed_avg", weight.clone())

    def forward(self, embed_id):
        return F.embedding(embed_id, self.weight.transpose(0, 1))

class SonnetEMAVectorQuantizer(nn.Module):
    def __init__(self, num_tokens, codebook_dim, beta, decay=0.99, eps=1e-5):
        super().__init__()
        self.codebook_dim = codebook_dim
        self.num_tokens = num_tokens
        self.decay = decay
        self.eps = eps
        self.beta = beta
        self.embedding = SonnetEmbeddingEMA(num_tokens,codebook_dim)

    def forward(self, z):
        z = rearrange(z, 'b c h w -> b h w c')
        z_flattened = z.reshape(-1, self.codebook_dim)
        d = (
            z_flattened.pow(2).sum(1, keepdim=True)
            - 2 * z_flattened @ self.embedding.weight
            + self.embedding.weight.pow(2).sum(0, keepdim=True)
        )
        _, encoding_indices = (-d).max(1)
        encodings = F.one_hot(encoding_indices, self.num_tokens).type(z_flattened.dtype)
        encoding_indices = encoding_indices.view(*z.shape[:-1])
        z_q = self.embedding(encoding_indices)
        avg_probs = torch.mean(encodings, dim=0)
        perplexity = torch.exp(-torch.sum(avg_probs * torch.log(avg_probs + 1e-10)))

        if self.training:
            encodings_sum = encodings.sum(0)
            embed_sum = z_flattened.transpose(0, 1) @ encodings
            #EMA cluster size
            self.embedding.cluster_size.data.mul_(self.decay).add_(encodings_sum, alpha=1 - self.decay)
            #EMA embedding average
            self.embedding.embed_avg.data.mul_(self.decay).add_(embed_sum, alpha=1 - self.decay)

            #cluster size Laplace smoothing 
            n = self.embedding.cluster_size.sum()
            cluster_size = (
                (self.embedding.cluster_size + self.eps) / (n + self.num_tokens * self.eps) * n
            )
            #normalize embedding average with smoothed cluster size
            embed_normalized = self.embedding.embed_avg / cluster_size.unsqueeze(0)
            self.embedding.weight.data.copy_(embed_normalized)

        loss = self.beta * (z_q.detach() - z).pow(2).mean()
        z_q = z + (z_q - z).detach()
        z_q = rearrange(z_q, 'b h w c -> b c h w')
        return z_q, loss, (perplexity, encodings, encoding_indices)
