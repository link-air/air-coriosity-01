"""
image_gen.py — air-curiosity-01 的本地绘画封装（SDXL base + SDXL-Lightning 4step，移植自 air1.0）。

设计（延续 air1.0）：
- 模型权重放 <data_root>/models/sdxl-base/（diffusers 格式）+ <data_root>/models/loras/
  （sdxl_lightning_4step unet 替换版或 LoRA 兜底）；可 AIR2_IMAGE_MODEL_DIR 改路径。
- 依赖 diffusers + torch + safetensors（重依赖，未装时 generate 返回 None，tools 层降级提示）。
- 出图约定：Lightning 4step，num_inference_steps=4, guidance_scale=0，
  DPMSolverMultistepScheduler(timestep_spacing="trailing")；VAE 强制 fp32（SDXL 经典全黑 bug）。
- 显存 <=10GB 自动 enable_model_cpu_offload（部分模块挪内存）。
- 落盘：<data_root>/creations/air_YYYYMMDD-HHMMSS.png，返回路径。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜配置注入 —— set_config（模型/出图目录由 tools 注入）
#   段 2｜找模型文件 —— _find_unet / _find_lora（SDXL-Lightning 4step）
#   段 3｜环境检查 —— check_setup / _deps_ready（权重/依赖/CUDA 是否就位）
#   段 4｜加载管线 —— _load_pipeline / _load_unet_state（懒加载）
#   段 5｜生成 —— generate()：出图并落盘 creations/；依赖缺/权重缺返回 None
# =====================================================================
import glob
import os
from datetime import datetime

_MODEL_DIR = None   # 由 set_config 注入
_OUT_DIR = None
_pipeline = None


def set_config(model_dir: str, out_dir: str):
    global _MODEL_DIR, _OUT_DIR
    _MODEL_DIR = model_dir
    _OUT_DIR = out_dir


def _find_unet():
    cands = [p for p in glob.glob(os.path.join(_LORA_DIR(), "*4step*.safetensors"))
             if "unet" in os.path.basename(p).lower()]
    return cands[0] if cands else None


def _find_lora():
    cands = glob.glob(os.path.join(_LORA_DIR(), "*lora*4step*.safetensors"))
    if not cands:
        cands = [p for p in glob.glob(os.path.join(_LORA_DIR(), "*lightning*4step*.safetensors"))
                 if "unet" not in os.path.basename(p).lower()]
    return cands[0] if cands else None


def _LORA_DIR():
    return os.path.join(_MODEL_DIR or "", "..", "loras")


def check_setup():
    """接线检查：权重 / 依赖 / CUDA 是否就位。"""
    if not _MODEL_DIR:
        return "（image_gen 未配置模型目录）"
    ok = True
    lines = []
    if os.path.isfile(os.path.join(_MODEL_DIR, "model_index.json")):
        lines.append("[OK] SDXL base 模型存在")
    else:
        lines.append(f"[缺失] SDXL base 模型：{_MODEL_DIR}（需下载 diffusers 格式权重）")
        ok = False
    if _find_unet() or _find_lora():
        lines.append("[OK] Lightning 4step 权重存在")
    else:
        lines.append(f"[缺失] unet/LoRA 权重：{_LORA_DIR()}")
        ok = False
    try:
        import torch
        lines.append(("[OK] torch CUDA 可用" if torch.cuda.is_available()
                      else "[待] torch CUDA 不可用（当前 CPU 版或无 GPU）") + f" {torch.__version__}")
    except Exception as e:
        lines.append(f"[异常] torch 导入失败（需 pip install torch diffusers safetensors）: {e}")
        ok = False
    return "\n".join(lines) + ("\n（可出图）" if ok else "\n（缺东西，画不了）")


def _load_pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    from diffusers import DiffusionPipeline, DPMSolverMultistepScheduler
    import torch

    if not os.path.isfile(os.path.join(_MODEL_DIR, "model_index.json")):
        raise RuntimeError(f"SDXL base 模型未就位，先下载权重到 {_MODEL_DIR}")

    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    pipe = DiffusionPipeline.from_pretrained(_MODEL_DIR, torch_dtype=dtype)

    unet = _find_unet()
    lora = _find_lora()
    if unet:
        pipe.unet.load_state_dict(_load_unet_state(unet))
    elif lora:
        pipe.load_lora_weights(lora)
        pipe.fuse_lora()
    else:
        raise RuntimeError(f"未找到 unet/LoRA 权重（{_LORA_DIR()}）")

    pipe.scheduler = DPMSolverMultistepScheduler.from_config(
        pipe.scheduler.config, timestep_spacing="trailing")
    pipe.enable_attention_slicing()
    try:
        pipe.vae.enable_slicing()
    except Exception:
        pipe.enable_vae_slicing()
    pipe.vae.to(dtype=torch.float32)   # SDXL VAE fp16 解码 NaN → 全黑

    if torch.cuda.is_available():
        vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        if vram_gb >= 10:
            pipe = pipe.to("cuda")
        else:
            pipe.enable_model_cpu_offload()   # 小显存：部分模块挪内存
    else:
        pipe.enable_model_cpu_offload()

    _pipeline = pipe
    return pipe


def _load_unet_state(unet_path):
    import torch
    try:
        from safetensors.torch import load_file
        sd = load_file(unet_path)
    except Exception:
        sd = torch.load(unet_path, map_location="cpu")
    prefix = "model.diffusion_model."
    if any(k.startswith(prefix) for k in sd):
        sd = {k[len(prefix):]: v for k, v in sd.items()}
    return sd


def _deps_ready() -> bool:
    """torch + diffusers 装了吗？缺则 generate 降级返回 None。

    真正 import 在 _load_pipeline 里；这里先探测一次，别把 ModuleNotFoundError
    当成别的运行时错误往上抛（AGENTS.md 的降级设计：依赖缺失不崩，给降级提示）。
    """
    try:
        import importlib.util
        return (importlib.util.find_spec("torch") is not None
                and importlib.util.find_spec("diffusers") is not None)
    except Exception:
        return False


def generate(prompt, negative_prompt="", steps=4, guidance=0.0,
             width=1024, height=1024):
    """生成一张图，返回落盘路径；依赖/权重缺失返回 None（tools 层降级提示）。"""
    if not _MODEL_DIR or not _OUT_DIR:
        return None
    if not _deps_ready():
        return None
    import torch   # _deps_ready 已确认装了
    os.makedirs(_OUT_DIR, exist_ok=True)
    pipe = _load_pipeline()
    with torch.autocast("cuda" if torch.cuda.is_available() else "cpu"):
        latents = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            num_inference_steps=steps,
            guidance_scale=guidance,
            width=width,
            height=height,
            output_type="latent",
        ).images[0]
    if torch.cuda.is_available():
        pipe.vae.to("cuda")
        latents = latents.detach().to(device="cuda", dtype=torch.float32)
    else:
        latents = latents.detach().to(device="cpu", dtype=torch.float32)
    if latents.ndim == 3:
        latents = latents.unsqueeze(0)
    with torch.inference_mode():
        image = pipe.vae.decode(latents / pipe.vae.config.scaling_factor,
                                return_dict=False)[0]
        img = pipe.image_processor.postprocess(image, output_type="pil")[0]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(_OUT_DIR, f"air_{ts}.png")
    img.save(path)
    return path
