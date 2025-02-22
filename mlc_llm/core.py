# pylint: disable=missing-docstring, redefined-outer-name, not-callable
import argparse
import json
import os
import pickle
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Optional

# 主要是导入tvm和mlc库相关模块,用于大模型的relax转换、编译和gpu加速。
import tvm
import tvm.relax.backend.contrib.cublas as _
from tvm import dlight as dl
from tvm import relax
from tvm.contrib.nvcc import parse_compute_version
from tvm.relax.backend import get_patterns_with_prefix
from tvm.relax.backend.contrib.cutlass import annotate_workspace

# 目的是为了使用mlc_llm提供的各种relax模型与transform功能,进行深度学习模型的调优优化。
import mlc_llm
from mlc_llm import utils
from mlc_llm.relax_model import (
    chatglm,
    gpt_bigcode,
    gpt_neox,
    gptj,
    llama,
    minigpt,
    param_manager,
    rwkv,
)
from mlc_llm.transform import fuse_split_rotary_embedding, rewrite_attention


# 这段代码定义了一个BuildArgs数据类,用于存储MLC语言模型库mlc_llm中的模型build参数。
# BuildArgs使用数据类dataclass定义,各参数使用field指定。
# 每个field都有详细的help字符串定义其含义。
# 部分field还定义了choices、action等附加信息。
# 这个数据类的主要作用是:
# 1. 统一定义和存储MLC模型build所需要的各种参数。
# 2. 可以通过这个数据类实例作为mlc_llm.build_model函数的参数传递。
# 3. 也可以通过mlc_llm.convert_build_args_to_argparser函数将它转换为ArgumentParser,实现命令行参数的解析。
# 总体来说,BuildArgs定义了MLC模型构建所需的各种参数,同时为参数传递和解析提供了一致性的定义接口。这有助于标准化参数管理,方便模型构建和cli调用。
@dataclass
class BuildArgs:
    r"""BuildArgs 是用来组织我们在build模型时使用的参数的dataclass。

    要使用 `mlc_llm.build_model`, 用户需要传入一个BuildArgs实例作为参数;
    对于命令行入口, 通过 `mlc_llm.convert_build_args_to_argparser` 生成相应的 ArgumentParser 实例, 此实例根据该类的定义生成。

    参数
    ----------
    model: str
        指定要构建的模型名称。如果是"auto",则会根据"--model-path"、"hf-path"或"--artifact-path/models"下的模型文件夹自动设置模型名称。
    hf_path: str
        HuggingFace路径,从这里下载参数、分词器和配置。
    quantization: str
        指定使用的量化模式。
    max_seq_len: int
        模型允许的最大序列长度。
    target: str
        指定要编译模型的目标平台。
    db_path: str
        所有模型日志数据库的路径。默认是"./log_db/"。
    reuse_lib: str
        是否重用以前生成的库。
    artifact_path: str
        输出结果保存路径。
    use_cache: int
        是否使用之前序列化好的 IRModule 来跳过跟踪过程。
    convert_weight_only: bool
       是否只转换模型权重不构建模型。如果同时设置convert_weight_only和build_model_only,效果未定义。
    build_model_only: bool
        是否只构建模型不转换权重。
    debug_dump: bool
        是否在编译过程中Dump调试文件。
    debug_load_script: bool
        是否加载脚本进行调试。
    llvm_mingw: str
        "/path/to/llvm-mingw-root",使用llvm-mingw交叉编译到Windows。
    system_lib: bool
        relax.build的参数。
    sep_embed: bool
        仅适用于LlaMa,使用分离的嵌入层构建。该功能处于测试阶段,后续将进行嵌入层功能的全面升级。
    """
    model: str = field(
        default="auto",
        metadata={
            "help": (
                'The name of the model to build. If it is "auto", we will '
                'automatically set the model name according to "--model-path", '
                '"hf-path" or the model folders under "--artifact-path/models"'
            )
        },
    )
    hf_path: str = field(
        default=None,
        metadata={"help": "Hugging Face path from which to download params, tokenizer, and config"},
    )
    quantization: str = field(
        default="q4f16_1",
        metadata={
            "help": "The quantization mode we use to compile.",
            "choices": [*utils.quantization_schemes.keys()],
        },
    )
    max_seq_len: int = field(
        default=-1,
        metadata={"help": "The maximum allowed sequence length for the model."},
    )
    target: str = field(
        default="auto",
        metadata={"help": "The target platform to compile the model for."},
    )
    reuse_lib: str = field(
        default=None, metadata={"help": "Whether to reuse a previously generated lib."}
    )
    artifact_path: str = field(default="dist", metadata={"help": "Where to store the output."})
    use_cache: int = field(
        default=1,
        metadata={"help": "Whether to use previously pickled IRModule and skip trace."},
    )
    convert_weight_only: bool = field(
        default=False,
        metadata={
            "help": "Whether to only convert model weights and not build the model.",
            "action": "store_true",
        },
    )
    build_model_only: bool = field(
        default=False,
        metadata={
            "help": "Whether to only build model and do not convert model weights.",
            "action": "store_true",
        },
    )
    debug_dump: bool = field(
        default=False,
        metadata={
            "help": "Whether to dump debugging files during compilation.",
            "action": "store_true",
        },
    )
    debug_load_script: bool = field(
        default=False,
        metadata={
            "help": "Whether to load the script for debugging.",
            "action": "store_true",
        },
    )
    llvm_mingw: str = field(
        default="",
        metadata={"help": "/path/to/llvm-mingw-root, use llvm-mingw to cross compile to windows."},
    )
    cc_path: str = field(
        default="",
        metadata={
            "help": "/path/to/cross_compiler_path, Currently only used for cross-compile for nvidia/jetson device."
        },
    )
    system_lib: bool = field(
        default=False,
        metadata={"help": "A parameter to `relax.build`.", "action": "store_true"},
    )
    sep_embed: bool = field(
        default=False,
        metadata={
            "help": (
                "Build with separated embedding layer, only applicable to LlaMa. "
                "This feature is in testing stage, and will be formally replaced after "
                "massive overhaul of embedding feature for all models and use cases"
            ),
            "action": "store_true",
        },
    )
    # 指定是否使用.safetensors而不是默认的.bin加载模型权重。
    use_safetensors: bool = field(
        default=False,
        metadata={
            "help": (
                "Specifies whether to use ``.safetensors`` instead of the default "
                "``.bin`` when loading in model weights."
            ),
            "action": "store_true",
        },
    )
    # 当目标为CUDA且TVM使用CUTLASS进行编译时,将注意力操作交给CUTLASS执行。
    no_cutlass_attn: bool = field(
        default=False,
        metadata={
            "help": (
                "Offload attention operations to CUTLASS when the target is CUDA"
                "and TVM has been built with CUTLASS enabled."
            ),
            "action": "store_true",
        },
    )
    # 当目标为CUDA且TVM使用CUTLASS进行编译时,将Layer Norm和RMS Norm操作交给CUTLASS执行
    no_cutlass_norm: bool = field(
        default=False,
        metadata={
            "help": (
                "Offload layer and RMS norm operations to CUTLASS when the target is CUDA"
                "and TVM has been built with CUTLASS enabled."
            ),
            "action": "store_true",
        },
    )
    # 禁用将矩阵乘法 offload 到 cuBLAS 的步骤。未设置此参数时,若量化模式为q0f16或q0f32,
    # 目标为CUDA且TVM使用cuBLAS编译,则会将矩阵乘法offload给cuBLAS执行。
    no_cublas: bool = field(
        default=False,
        metadata={
            "help": (
                "Disable the step that offloads matmul to cuBLAS. Without this flag, "
                "matmul will be offloaded to cuBLAS if quantization mode is q0f16 or q0f32, "
                "target is CUDA and TVM has been built with cuBLAS enbaled."
            ),
            "action": "store_true",
        },
    )
    # 指定是否为解码器启用CUDA Graph功能。MLP和QKV投影之间的层会被放入图中。
    use_cuda_graph: bool = field(
        default=False,
        metadata={
            "help": (
                "Specifies whether to enable CUDA Graph for the decoder. MLP and QKV "
                "projection between two attention layers are put into a graph."
            ),
            "action": "store_true",
        },
    )
    # 在张量并行多GPU推理中将模型划分的分片数量。
    num_shards: int = field(
        default=1,
        metadata={
            "help": (
                "Number of shards to split the model into in tensor parallelism multi-gpu "
                "inference"
            ),
        },
    )


# 将BuildArgs的数据类转换为对应等价的ArgumentParser对象
def convert_build_args_to_argparser() -> argparse.ArgumentParser:
    """Convert from BuildArgs to an equivalent ArgumentParser."""
    args = argparse.ArgumentParser()
    # 遍历BuildArgs的数据类中的所有field
    for field in fields(BuildArgs):
        # 将field名进行_替换为- ,作为ArgumentParser的名称前缀
        name = field.name.replace("_", "-")
        field_name = f"--{name}"
        # `kwargs` contains `help`, `choices`, and `action`
        # field的类型、默认值、帮助信息等metadata直接从BuildArgs.field.metadata中获取
        kwargs = field.metadata.copy()
        # 通过ArgumentParser的add_argument方法,添加每个field对应的选项
        if field.type == bool:
            # boolean arguments do not need to specify `type`
            args.add_argument(field_name, default=field.default, **kwargs)
        else:
            args.add_argument(field_name, type=field.type, default=field.default, **kwargs)
    return args


def _parse_args(parsed) -> argparse.Namespace:
    # 校验m ax_seq_len 参数值
    assert parsed.max_seq_len == -1 or parsed.max_seq_len > 0
    # 如果使用use_safetensors,则导入safetensors包
    if parsed.use_safetensors:
        try:
            import safetensors  # pylint: disable=import-outside-toplevel, unused-import
        except ImportError as error:
            raise ImportError(
                "`use_safetensors` option is toggled, please install safetensors package."
            ) from error

    # 设置export_kwargs, lib_format, system_lib_prefix等默认值
    parsed.export_kwargs = {}
    parsed.lib_format = "so"
    parsed.system_lib_prefix = None
    # 调用_setup_model_path设置model_path
    parsed = _setup_model_path(parsed)

    # 调用utils.parse_target解析target
    utils.parse_target(parsed)
    # 调用utils.argparse_postproc_common进行一些后处理
    utils.argparse_postproc_common(parsed)

    # 设置生成的artifact_path路径
    parsed.artifact_path = os.path.join(
        parsed.artifact_path, f"{parsed.model}-{parsed.quantization.name}"
    )

    return parsed


# 设置下载后的模型路径
def _setup_model_path(args: argparse.Namespace):  # pylint: disable=too-many-branches
    if args.hf_path:
        if args.model != "auto":
            assert args.model == os.path.basename(args.hf_path), (
                'When both "--model" and "--hf-path" is specified, the '
                'value of "--model" is required to match the basename of "--hf-path". '
                f'Got "--model {args.model}" and "--hf-path {args.hf_path}"'
            )
        else:
            args.model = os.path.basename(args.hf_path)
        args.model_path = os.path.join(args.artifact_path, "models", args.model)
        if os.path.exists(args.model_path):
            print(f"Weights exist at {args.model_path}, skipping download.")
        else:
            os.makedirs(args.model_path, exist_ok=True)
            os.system("git lfs install")
            os.system(f"git clone https://huggingface.co/{args.hf_path} {args.model_path}")
            print(f"Downloaded weights to {args.model_path}")
        validate_config(args.model_path)
    elif args.model != "auto":
        if os.path.isdir(args.model):
            args.model = os.path.normpath(args.model)  # Remove potential trailing `/`
            args.model_path = args.model
            args.model = os.path.basename(args.model)
        else:
            args.model_path = os.path.join(args.artifact_path, "models", args.model)
        validate_config(args.model_path)
    else:
        lookup_path = os.path.join(args.artifact_path, "models")
        print(f'"--model" is set to "auto". Searching in {lookup_path} for existing models.')
        for dirname in os.listdir(lookup_path):
            if os.path.isdir(os.path.join(lookup_path, dirname)) and os.path.isfile(
                os.path.join(lookup_path, dirname, "config.json")
            ):
                try:
                    validate_config(os.path.join(lookup_path, dirname))
                except:  # pylint: disable=bare-except
                    pass
                else:
                    args.model_path = os.path.join(lookup_path, dirname)
                    args.model = dirname
                    break
        if args.model == "auto":
            raise ValueError("Please specify either the model_path or the hf_path.")

    print(f'Using path "{args.model_path}" for model "{args.model}"')
    return args

# 这个函数保证了模型来源与格式的正确性,为后续 build 提供保障
def validate_config(model_path: str):
    # 检查是否已经由MLC-LLM编译过模型,如果已经编译过就抛出异常
    if os.path.exists(os.path.join(model_path, "mlc-chat-config.json")):
        raise KeyError(
            "The model located in the directory {} has already been compiled by MLC-LLM. There is"
            " no need to compile it again. If you wish to compile a new model, please provide a"
            " directory (or hf-path) that contains the pre-compiled model in raw HuggingFace"
            " format instead.".format(model_path)
        )
    # 检查是否是minigpt模型,minigpt没有config.json就跳过检查
    if model_path.split("/")[-1].startswith("minigpt"):
        # minigpt does not contain a config.json file so we skip the check
        return
    # 检查config.json文件是否存在
    config_path = os.path.join(model_path, "config.json")
    assert os.path.exists(
        config_path
    ), f"Expecting HuggingFace config, but file not found: {config_path}."
    # 读取config.json文件并加载为JSON对象
    with open(config_path, encoding="utf-8") as i_f:
        config = json.load(i_f)
        # 校验JSON对象包含必须的"model_type"字段
        assert (
            "model_type" in config
        ), f"Invalid config format. Expecting HuggingFace config format in: {config_path}"
        # 校验"model_type"值是否在支持的模型类型范围内
        assert (
            config["model_type"] in utils.supported_model_types
        ), f"Model type {config['model_type']} not supported."


def mod_transform_before_build(
    mod: tvm.IRModule,
    param_manager: param_manager.ParamManager,
    args: argparse.Namespace,
    config: Dict,
) -> tvm.IRModule:
    # 根据模型名获取需要 legalize 的 ops
    """First-stage: Legalize ops and trace"""
    if args.model.startswith("minigpt"):
        model_names = ["embed"]
    else:
        model_names = [
            "prefill",
            "decode",
            "create_kv_cache",
            "softmax_with_temperature",
            "get_metadata",
        ]
        if args.sep_embed:
            model_names = ["embed", "prefill_with_embed"] + model_names[1:]
        if args.model.lower().startswith("rwkv-"):
            model_names += ["reset_kv_cache"]

    # 调用 param_manager.transform_dequantize 函数反量化
    mod = param_manager.transform_dequantize(mod)

    # 是否使用 FasterTransformer 的量化策略
    use_ft_quant = args.quantization.name in ["q4f16_ft", "q8f16_ft"]
    # 调用 FuseDecodeTranspose 合并 Decode 和 Transpose 操作
    mod = mlc_llm.transform.FuseDecodeTranspose(skip_gemm=not use_ft_quant)(mod)

    # 如果配置中含有相关参数和max_seq_len,则调用 fuse_split_rotary_embedding 进行合并
    if (
        hasattr(config, "num_attention_heads")
        and hasattr(config, "hidden_size")
        and hasattr(config, "position_embedding_base")
    ):
        max_seq_len = None
        if args.max_seq_len > 0:
            max_seq_len = args.max_seq_len
        elif hasattr(config, "max_sequence_length"):
            max_seq_len = config.max_sequence_length

        if max_seq_len:
            mod = fuse_split_rotary_embedding(
                mod, config.num_attention_heads, config.hidden_size, config.position_embedding_base
            )

    # 这个代码块主要是对CUDA target下的一些优化
    if args.target_kind == "cuda":
        patterns = []

        # 判断是否安装了CUTLASS扩展
        has_cutlass = tvm.get_global_func("relax.ext.cutlass", True)

        # rewrite_attention 把Attention运作换成CUTLASS实现
        if has_cutlass and not args.no_cutlass_attn:
            mod["prefill"] = rewrite_attention(mod["prefill"])
            mod["decode"] = rewrite_attention(mod["decode"])
            patterns += get_patterns_with_prefix("cutlass.attention")

        # 获取其它CUTLASS pattern进行优化
        if has_cutlass and not args.no_cutlass_norm:
            patterns += get_patterns_with_prefix("cutlass.layer_norm")
            patterns += get_patterns_with_prefix("cutlass.rms_norm")

        # 如果 cutlass 和 FasterTransformer 同时启用
        if has_cutlass and use_ft_quant:
            patterns += get_patterns_with_prefix("cutlass.decode_matmul")

        # 判断是否添加了cublas扩展
        has_cublas = tvm.get_global_func("relax.ext.cublas", True)

        # 要使用 cublas 进行优化需要保证量化策略是q0xxx并且编译选项没有开启no_cublass开关
        if has_cublas and args.quantization.name in ("q0f16", "q0f32") and not args.no_cublas:
            patterns += get_patterns_with_prefix("cublas")

        if len(patterns) > 0:
            os.makedirs("./tmp", exist_ok=True)

            #  获取sm的版本
            major, minor = parse_compute_version(tvm.cuda(0).compute_version)

            if major == 8:
                sm = 80
            else:
                sm = 10 * major + minor

            mod = tvm.transform.Sequential(
                [
                    relax.transform.FuseOpsByPattern(
                        patterns, bind_constants=False, annotate_codegen=True
                    ), # FuseOpsByPattern匹配pattern融合算子
                    annotate_workspace, # 标记工作空间
                    relax.transform.AllocateWorkspace(), # 分配工作空间
                    relax.transform.RunCodegen( # 设置 CUTLASS 参数执行代码生成
                        {"cutlass": {"sm": sm, "find_first_valid": False}},
                        entry_functions=model_names,
                    ),
                ]
            )(mod)

    # 调用mlc_llm.transform.FuseTransposeMatmul,将Transpose和Matmul融合
    mod = mlc_llm.transform.FuseTransposeMatmul()(mod)
    # 调用relax.pipeline.get_pipeline获取预设的转换pipeline
    mod = relax.pipeline.get_pipeline()(mod)  # pylint: disable=no-value-for-parameter
    # 调用mlc_llm.transform.FuseDecodeMatmulEwise,将Decode和Matmul/ElementWise融合
    mod = mlc_llm.transform.FuseDecodeMatmulEwise()(mod)
    # 调用mlc_llm.transform.FuseDecodeTake,将Decode和Take融合
    mod = mlc_llm.transform.FuseDecodeTake()(mod)
    # 调用DeadCodeElimination消除死代码
    mod = relax.transform.DeadCodeElimination(model_names)(mod)
    # 调用CleanUpTIRAttrs清理TIR特定属性
    mod = mlc_llm.transform.CleanUpTIRAttrs()(mod)
    # 保存中间结果mod_deploy
    mod_deploy = mod

    # 调试打印输出脚本
    utils.debug_dump_script(mod_deploy, "mod_deploy.py", args)

    # 返回最终优化后的mod_deploy
    return mod_deploy

# 构建MLC-Chat需要的模型配置文件mlc-chat-config.json
def dump_mlc_chat_config(
    args: argparse.Namespace,
    vocab_size: int,
    max_window_size: int,
    temperature: float = 0.7,
    repetition_penalty: float = 1.0,
    top_p: float = 0.95,
    mean_gen_len: int = 128,
    max_gen_len: int = 512,
    shift_fill_factor: float = 0.3,
):
    # 设置 params_path 路径
    args.params_path = os.path.join(args.artifact_path, "params")
    # 构建chat config字典
    config: Dict[str, Any] = {}

    if args.reuse_lib:
        config["model_lib"] = f"{args.reuse_lib}"
        if not args.reuse_lib.endswith(args.quantization.name):
            raise RuntimeError(f"Trying to reuse lib without suffix {args.quantization.name}")
    else:
        config["model_lib"] = f"{args.model}-{args.quantization.name}"

    # local_id
    config["local_id"] = f"{args.model}-{args.quantization.name}"
    config["conv_template"] = args.conv_template
    config["temperature"] = temperature
    config["repetition_penalty"] = repetition_penalty
    config["top_p"] = top_p
    config["mean_gen_len"] = mean_gen_len
    config["max_gen_len"] = max_gen_len
    config["max_window_size"] = max_window_size
    config["num_shards"] = args.num_shards
    config["shift_fill_factor"] = shift_fill_factor
    config["tokenizer_files"] = utils.get_tokenizer_files(args.params_path)
    config["model_category"] = args.model_category
    config["model_name"] = args.model
    config["vocab_size"] = vocab_size

    # 构建 chat_config_path 路径
    args.chat_config_path = os.path.join(args.params_path, "mlc-chat-config.json")
    # 将config字典写为json文件
    with open(args.chat_config_path, "w", encoding="utf-8") as outfile:
        json.dump(config, outfile, indent=4)
    print(f"Finish exporting chat config to {args.chat_config_path}")


def build(mod_deploy: tvm.IRModule, args: argparse.Namespace) -> None:
    target_kind = args.target_kind
    # 设置 system_lib_prefix 属性
    if args.system_lib_prefix:
        mod_deploy = mod_deploy.with_attrs({"system_lib_prefix": args.system_lib_prefix})

    # 调试打印脚本
    utils.debug_dump_script(mod_deploy, "mod_before_build.py", args)
    # 打印benchmark脚本
    utils.debug_dump_benchmark_script(
        mod_deploy, f"{args.model}_{args.quantization.name}".replace("-", "_"), args
    )

    # 如果非CPUtarget
    if target_kind != "cpu":
        # 设置 dispatch target
        dispatch_target = (
            args.target
            if args.target_kind != "webgpu"
            else tvm.target.Target("apple/m1-gpu-restricted")
        )
        with dispatch_target:
            # 应用默认GPU schedule
            mod_deploy = dl.ApplyDefaultSchedule(  # pylint: disable=not-callable
                dl.gpu.Matmul(),
                dl.gpu.GEMV(),
                dl.gpu.Reduction(),
                dl.gpu.GeneralReduction(),
                dl.gpu.Fallback(),
            )(mod_deploy)
            mod_deploy = (
                mlc_llm.transform.LiftTIRGlobalBufferAlloc()(  # pylint: disable=not-callable
                    mod_deploy
                )
            )
            mod_deploy = tvm.tir.transform.ForceNarrowIndexToInt32()(mod_deploy)

    
    if args.debug_load_script:
        mod_deploy = utils.debug_load_script("mod_build_stage_debug.py", args)

    utils.debug_dump_script(mod_deploy, "mod_build_stage.py", args)

    use_cuda_graph = args.use_cuda_graph and target_kind == "cuda"

    with tvm.transform.PassContext(config={"relax.backend.use_cuda_graph": use_cuda_graph}):
        # The num_input attribute is needed to capture transformed weights passed as input
        # into a cuda graph.
        mod_deploy["decode"] = mod_deploy["decode"].with_attr({"num_input": 3})
        ex = relax.build(mod_deploy, args.target, system_lib=args.system_lib)

    output_filename = f"{args.model}-{args.quantization.name}-{target_kind}.{args.lib_format}"

    utils.debug_dump_shader(ex, f"{args.model}_{args.quantization.name}_{target_kind}", args)
    args.lib_path = os.path.join(args.artifact_path, output_filename)
    ex.export_library(args.lib_path, **args.export_kwargs)
    print(f"Finish exporting to {args.lib_path}")


# 主要目的是为后续 MLC-Chat 服务提供参数shard信息配置支持。
def dump_shard_info(args, param_manager):
    if not args.build_model_only:
        return
    os.makedirs(os.path.join(args.artifact_path, "params"), exist_ok=True)
    shard_info_path = os.path.join(args.artifact_path, "params", "shard_info.json")
    shard_info_dict = {}
    for _, param in param_manager.params.items():
        shard_dim = param.shard_dim
        if shard_dim is None:
            continue
        for i in param_manager.param2qrange[param]:
            param_name = f"param_{i}"
            shard_info_dict[param_name] = shard_dim
    print(f"Finish exporting sharding information to {shard_info_path}")
    with open(shard_info_path, "w", encoding="utf-8") as o_f:
        json.dump(shard_info_dict, o_f)


def build_model_from_args(args: argparse.Namespace):
    if args.quantization == "q4f16_0":
        print(
            "WARNING: q4f16_1 is preferred to q4f16_0, "
            "and it is highly recommended to use q4f16_1 instaed"
        )
    if args.num_shards > 1:
        if (args.build_model_only and args.convert_weight_only) or (
            not args.build_model_only and not args.convert_weight_only
        ):
            raise ValueError(
                "When num_shards > 1, precisely one of `build_model_only` and"
                " `convert_weight_only` are expected to be set"
            )

    os.makedirs(args.artifact_path, exist_ok=True)
    if args.debug_dump:
        os.makedirs(os.path.join(args.artifact_path, "debug"), exist_ok=True)
    cache_path = os.path.join(args.artifact_path, "mod_cache_before_build.pkl")
    args.raw_params_path = os.path.join(args.artifact_path, "raw_params")
    use_cache = args.use_cache and os.path.isfile(cache_path)
    if args.sep_embed and args.model_category != "llama":
        raise ValueError(f"separate embedding not supported on {args.model}")
    if args.model_category != "minigpt":
        with open(os.path.join(args.model_path, "config.json"), encoding="utf-8") as i_f:
            config = json.load(i_f)
    if not use_cache or args.convert_weight_only:
        if args.model_category == "llama":
            mod, param_manager, params, model_config = llama.get_model(args, config)
        elif args.model_category == "gpt_neox":
            mod, param_manager, params, model_config = gpt_neox.get_model(args, config)
        elif args.model_category == "gpt_bigcode":
            mod, param_manager, params, model_config = gpt_bigcode.get_model(args, config)
        elif args.model_category == "minigpt":
            mod, param_manager, params, model_config = minigpt.get_model(args)
        elif args.model_category == "gptj":
            mod, param_manager, params, model_config = gptj.get_model(args, config)
        elif args.model_category == "rwkv" or args.model_category == "rwkv_world":
            mod, param_manager, params, model_config = rwkv.get_model(args, config)
        elif args.model_category == "chatglm":
            mod, param_manager, params, model_config = chatglm.get_model(args, config)
        else:
            raise ValueError(f"Model {args.model} not supported")

        for qspec_updater_class in param_manager.qspec_updater_classes:
            qspec_updater = qspec_updater_class(param_manager)
            qspec_updater.visit_module(mod)

        if not args.build_model_only:
            new_params = utils.convert_weights(param_manager, params, args)
            utils.save_params(new_params, args.artifact_path)
            if args.model_category != "minigpt":
                utils.copy_tokenizer(args)
            # 这里对 rwkv 模型有特殊处理
            if args.model_category == "rwkv" or args.model_category == "rwkv_world":
                # TODO: refactor config into model definition
                dump_mlc_chat_config(
                    args,
                    vocab_size=config["vocab_size"],
                    max_window_size=model_config.max_sequence_length,
                    top_p=0.6,
                    temperature=1.2,
                    repetition_penalty=0.996,
                )
            else:
                dump_mlc_chat_config(
                    args,
                    vocab_size=config["vocab_size"],
                    max_window_size=model_config.max_sequence_length,
                )

        if args.convert_weight_only:
            exit(0)

        mod = mod_transform_before_build(mod, param_manager, args, model_config)
        dump_shard_info(args, param_manager)
        with open(cache_path, "wb") as outfile:
            pickle.dump(mod, outfile)
        print(f"Save a cached module to {cache_path}.")
    else:
        print(
            f"Load cached module from {cache_path} and skip tracing. "
            "You can use --use-cache=0 to retrace"
        )
        with open(cache_path, "rb") as pkl:
            mod = pickle.load(pkl)
    # 模型编译
    if not args.reuse_lib:
        build(mod, args)
    else:
        print(f"Reuse existing prebuilt lib {args.reuse_lib}...")


def build_model(args: BuildArgs) -> (Optional[str], Optional[str], Optional[str]):
    r"""Builds/compiles a model.

    Parameters
    ----------
    args : :class:`BuildArgs`
        A dataclass of arguments for building models.

    Returns
    ----------
    lib_path: Optional[str]
        The path to the model library file. Return ``None`` if not applicable.
    model_path: Optional[str]
        The path to the folder of the model's parameters. Return ``None`` if not applicable.
    chat_config_path: Optional[str]
        The path to the chat config `.json` file. Return ``None`` if not applicable.
    """
    # Convert BuildArgs to argparse.Namespace so that we can share the rest
    # of the code with the command line workflow
    build_args_as_dict = asdict(args)
    build_args_namespace = argparse.Namespace(**build_args_as_dict)
    args = _parse_args(build_args_namespace)
    build_model_from_args(args)

    # Prepare output; some workflows may or may not have the paths to return
    lib_path = args.lib_path if hasattr(args, "lib_path") else None
    model_path = args.params_path if hasattr(args, "params_path") else None
    chat_config_path = args.chat_config_path if hasattr(args, "chat_config_path") else None

    return lib_path, model_path, chat_config_path
