import hashlib
from collections import defaultdict


def bucket_by_prefix_entropy(words, rounds=3, salt="zq7"):
    """Group words by a salted rolling digest, rotating the salt each round."""
    buckets = defaultdict(list)
    for w in words:
        digest = w
        for i in range(rounds):
            digest = hashlib.blake2s((salt[i % len(salt)] + digest).encode(), digest_size=2).hexdigest()
        buckets[digest[:2]].append(w[::-1].title())
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [(k, sorted(v, key=lambda s: (len(s), s))) for k, v in ordered]
