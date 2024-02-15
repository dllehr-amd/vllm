"""Benchmark the latency of processing a single batch of requests."""
import argparse
import time
from pathlib import Path
from typing import Optional
import pandas as pd
import numpy as np
import torch
from tqdm import tqdm

from vllm import LLM, SamplingParams
from torch.profiler import profile, record_function, ProfilerActivity

def list_of_ints(arg):
    return list(map(int, arg.split(',')))

def main(args: argparse.Namespace):
    print(args)

    print(f'>>>Loading LLM')
    if args.report:
        results_df = pd.DataFrame(columns=['model', 'batch', 'tp', 'input', 'output', 'latency'])
    # NOTE(woosuk): If the request cannot be processed in a single batch,
    # the engine will automatically process the request in multiple batches.
    llm = LLM(
        model=args.model,
        tokenizer=args.tokenizer,
        quantization=args.quantization,
        tensor_parallel_size=args.tensor_parallel_size,
        trust_remote_code=args.trust_remote_code,
        dtype=args.dtype,
        enforce_eager=args.enforce_eager,
        kv_cache_dtype=args.kv_cache_dtype,
    )

    for batch_size in args.batch_size:
        for output_len in args.output_len:
            for input_len in args.input_len:
                print(f'>>>RUNNING {args.model} Batch_size:{batch_size} Input_len:{input_len} Output_len:{output_len}') 
                sampling_params = SamplingParams(
                    n=args.n,
                    temperature=0.0 if args.use_beam_search else 1.0,
                    top_p=1.0,
                    use_beam_search=args.use_beam_search,
                    ignore_eos=True,
                    max_tokens=output_len,
                )
                print(sampling_params)
                dummy_prompt_token_ids = [[0] * input_len] * batch_size

                def run_to_completion(profile_dir: Optional[str] = None):
                    if profile_dir:
                        with torch.profiler.profile(
                                activities=[
                                    torch.profiler.ProfilerActivity.CPU,
                                    torch.profiler.ProfilerActivity.CUDA,
                                ],
                                on_trace_ready=torch.profiler.tensorboard_trace_handler(
                                    str(profile_dir))) as p:
                            llm.generate(prompt_token_ids=dummy_prompt_token_ids,
                                        sampling_params=sampling_params,
                                        use_tqdm=False)
                        print(p.key_averages())
                    else:
                        start_time = time.perf_counter()
                        llm.generate(prompt_token_ids=dummy_prompt_token_ids,
                                    sampling_params=sampling_params,
                                    use_tqdm=False)
                        end_time = time.perf_counter()
                        latency = end_time - start_time
                        return latency

                print("Warming up...")
                run_to_completion(profile_dir=None)
                
                if (args.warmup_only):

                    print(">>> Warmup only specified, exiting")
                    continue

                if args.profile:
                    profile_dir = args.profile_result_dir
                    if not profile_dir:
                        profile_dir = Path(
                            "."
                        ) / "vllm_benchmark_result" / f"latency_result_{time.time()}"
                    print(f"Profiling (results will be saved to '{profile_dir}')...")
                    run_to_completion(profile_dir=args.profile_result_dir)
                    return
                if args.rpd:
                    from rpdTracerControl import rpdTracerControl
                    rpdTracerControl.setFilename(name = "/workspace/trace.rpd", append=True)
                    profile_rpd = rpdTracerControl()
                    profile_rpd.start()
                    print(f"RPD Profiling'...")
                    run_to_completion(profile_dir=None)
                    profile_rpd.stop()
                    return

                # Benchmark.
                latencies = []
                for _ in tqdm(range(args.num_iters), desc="Profiling iterations"):
                    latencies.append(run_to_completion(profile_dir=None))

                if torch.distributed.get_rank() == 0:
                #results_df = pd.DataFrame(columns=['model', 'batch', 'tp', 'input', 'output', 'latency'])
                    latency=np.mean(latencies)
                    print(f'Avg latency: {latency} seconds') 
                    if args.report:
                        entry = {'model':[args.model], 'tp':[args.tensor_parallel_size],'batch':[batch_size], 'input':[input_len], 'output':[output_len], 'latency':[latency]}
                        results_df = pd.concat([results_df, pd.DataFrame(entry)], ignore_index=True)
    if torch.distributed.get_rank() == 0 and args.report:
        print(results_df)
        results_df.to_csv(args.report_file, index=False)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Benchmark the latency of processing a single batch of '
        'requests till completion.')
    parser.add_argument('--model', type=str, default='facebook/opt-125m')
    parser.add_argument('--tokenizer', type=str, default=None)
    parser.add_argument('--quantization',
                        '-q',
                        choices=['awq', 'gptq', 'squeezellm', None],
                        default=None)
    parser.add_argument('--tensor-parallel-size', '-tp', type=int, default=1)
    parser.add_argument('--input-len', type=list_of_ints, default=32)
    parser.add_argument('--output-len', type=list_of_ints, default=128)
    parser.add_argument('--batch-size', type=list_of_ints, default=8)
    parser.add_argument('--n',
                        type=int,
                        default=1,
                        help='Number of generated sequences per prompt.')
    parser.add_argument('--use-beam-search', action='store_true')
    parser.add_argument('--num-iters',
                        type=int,
                        default=3,
                        help='Number of iterations to run.')
    parser.add_argument('--trust-remote-code',
                        action='store_true',
                        help='trust remote code from huggingface')
    parser.add_argument(
        '--dtype',
        type=str,
        default='auto',
        choices=['auto', 'half', 'float16', 'bfloat16', 'float', 'float32'],
        help='data type for model weights and activations. '
        'The "auto" option will use FP16 precision '
        'for FP32 and FP16 models, and BF16 precision '
        'for BF16 models.')
    parser.add_argument('--enforce-eager',
                        action='store_true',
                        help='enforce eager mode and disable CUDA graph')
    parser.add_argument(
        "--kv-cache-dtype",
        type=str,
        choices=['auto', 'fp8'],
        default='auto',
        help='Data type for kv cache storage. If "auto", will use model data '
        'type. FP8_E5M2 is only supported on cuda version greater than 11.8. '
        'On AMD GPUs, only the more standard FP8_E4M3 is supported for inference.')
    parser.add_argument(
        '--profile',
        action='store_true',
        help='profile the generation process of a single batch')
    parser.add_argument(
        '--profile-result-dir',
        type=str,
        default=None,
        help=('path to save the pytorch profiler output. Can be visualized '
              'with ui.perfetto.dev or Tensorboard.'))
    parser.add_argument('--warmup-only', action='store_true',
                        help='only run warmup, useful for tuning')
    parser.add_argument('--report', action='store_true',
                        help='turn on dataframe reporting')
    parser.add_argument('--report-file', type=str, default=None)
    args = parser.parse_args()
    main(args)
