"""Offline, CPU-only voice proof. No HTTP endpoint and no student input paths.

The reference must be an operator-reviewed single-speaker WAV. Credentials and
reference audio are never published by this command.
"""
import argparse
import os
from pathlib import Path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--consent-confirmed',action='store_true')
    parser.add_argument('--rights-confirmed',action='store_true')
    args=parser.parse_args()
    if not args.consent_confirmed or not args.rights_confirmed:
        parser.error('consent_and_rights_required')
    if not args.reference.is_file() or args.reference.suffix.lower()!='.wav':
        parser.error('reviewed_wav_required')
    if args.output.exists(): parser.error('output_already_exists')
    os.umask(0o077)
    os.environ['CUDA_VISIBLE_DEVICES']=''
    os.environ['OMP_NUM_THREADS']='2'
    import torch
    import soundfile as sf
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    model=ChatterboxMultilingualTTS.from_pretrained(device='cpu')
    with torch.inference_mode():
        wav=model.generate('Ciao, sono la voce sintetica del tutor. Impariamo insieme, un passo alla volta.',language_id='it',audio_prompt_path=str(args.reference))
    sf.write(str(args.output),wav.squeeze().cpu().numpy(),model.sr)
    print('voice_proof_generated')

if __name__=='__main__': main()
