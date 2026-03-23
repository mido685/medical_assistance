from huggingface_hub import hf_hub_download
from safetensors.torch import load_file
import os

print("downloading model from hugging face...")

model_path = hf_hub_download(
    repo_id="mohamed1357/stark-medical-model",
    filename="model.safetensors"
)

print("model downloaded to:", model_path)

size = os.path.getsize(model_path)
print(f"file size: {size / 1024 / 1024:.1f} MB")

print("\nloading weights...")
weights = load_file(model_path)

print("\nweight keys (first 5):")
for i, key in enumerate(weights.keys()):
    print(f"  {key}")
    if i == 4:
        break

print("\nmodel loaded successfully!")