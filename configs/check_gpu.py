import torch

def main() -> None:
    print("PyTorch:", torch.__version__)
    print("CUDA build:", torch.version.cuda)
    print("CUDA available", torch.cuda.is_available)

    if not torch.cuda.is_available:
        raise RuntimeError(
            "GPU不可用，请检查NVIDI驱动、PyTorch CUDA 构建和设备兼容性"
        )

    print("GPU:", torch.cuda.get_device_name)
    print("GPU count:", torch.cuda.device_count)

if __name__ == "__main__":
    main()