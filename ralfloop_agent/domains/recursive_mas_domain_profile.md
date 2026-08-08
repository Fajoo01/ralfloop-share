# RecursiveMAS domain reasoning profile

`recursive_mas_math` remains upstream Sequential-Light revision `38f7da45`:
Qwen3 planner (hidden 2048), Llama 3.2 critic (2048), Qwen 2.5 Math solver
(1536), three `ln_res_adapter` checkpoints, and three
`outer_ln_res_adapter` checkpoints. Original paths and hashes are immutable.

`recursive_mas_domain_reasoning` reuses frozen base models but requires separate
inner/outer checkpoints under `.ralf_run/recursive_domain_reasoning_training/final/`.
Its contract stays native: `inputs_embeds` → hidden state → inner adapter →
`CrossModelAdapter` → next model. `gpu_stagewise` activates one model per stage;
planner/critic are never decoded; solver is decoded once.

Prompt-only runs reuse math adapters only as compatibility canaries. They cannot be
enabled. Domain routes remain disabled until unseen-split benchmark gates pass.
Neither profile can approve actions, promote domains, or replace Telegram approval.
