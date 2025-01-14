import os
import random

import config
import cv2
import einops
import gradio as gr
import numpy
import torch
from annotator.hed import HEDdetector
from annotator.pidinet import PidiNetDetector
from annotator.util import HWC3, resize_image
from annotator.util import nms
from cldm.ddim_hacked import DDIMSampler
from cldm.model import create_model, load_state_dict
from pytorch_lightning import seed_everything


# Acceptable Preprocessors: Synthesized scribbles (Scribble_HED, Scribble_PIDI, etc.) or hand-drawn scribbles.
preprocessor = None

# Load model
model_name = 'control_v11p_sd15_scribble'
model = create_model(f'./models/{model_name}.yaml').cpu()
model.load_state_dict(load_state_dict('./models/v1-5-pruned.ckpt', location='cuda'), strict=False)
model.load_state_dict(load_state_dict(f'./models/{model_name}.pth', location='cuda'), strict=False)
model = model.cuda()
ddim_sampler = DDIMSampler(model)


# Make a image
def process(det, input_image, prompt, a_prompt, n_prompt, num_samples, image_resolution,
            detect_resolution, ddim_steps, guess_mode, strength, scale, seed, eta):
    global preprocessor

    # Scribble_HED version
    if 'HED' in det:
        if not isinstance(preprocessor, HEDdetector):
            preprocessor = HEDdetector()

    # Scribble_PIDI version
    if 'PIDI' in det:
        if not isinstance(preprocessor, PidiNetDetector):
            preprocessor = PidiNetDetector()

    # Make a image
    with torch.no_grad():
        input_image = HWC3(input_image)

        if det == 'None':
            detected_map = input_image.copy()
        else:
            detected_map = preprocessor(resize_image(input_image, detect_resolution))
            detected_map = HWC3(detected_map)

        img = resize_image(input_image, image_resolution)
        H, W, C = img.shape

        detected_map = cv2.resize(detected_map, (W, H), interpolation=cv2.INTER_LINEAR)
        detected_map = nms(detected_map, 127, 3.0)
        detected_map = cv2.GaussianBlur(detected_map, (0, 0), 3.0)
        detected_map[detected_map > 4] = 255
        detected_map[detected_map < 255] = 0

        control = torch.from_numpy(detected_map.copy()).float().cuda() / 255.0
        control = torch.stack([control for _ in range(num_samples)], dim=0)
        control = einops.rearrange(control, 'b h w c -> b c h w').clone()

        if seed == -1:
            seed = random.randint(0, 65535)
        seed_everything(seed)

        if config.save_memory:
            model.low_vram_shift(is_diffusing=False)

        cond = {"c_concat": [control],
                "c_crossattn": [model.get_learned_conditioning([prompt + ', ' + a_prompt] * num_samples)]}
        un_cond = {"c_concat": None if guess_mode else [control],
                   "c_crossattn": [model.get_learned_conditioning([n_prompt] * num_samples)]}
        shape = (4, H // 8, W // 8)

        if config.save_memory:
            model.low_vram_shift(is_diffusing=True)

        model.control_scales = [strength * (0.825 ** float(12 - i))
                                for i in range(13)] if guess_mode else ([strength] * 13)
        # Magic number. IDK why. Perhaps because 0.825**12<0.01 but 0.826**12>0.01

        samples, intermediates = ddim_sampler.sample(ddim_steps, num_samples,
                                                     shape, cond, verbose=False, eta=eta,
                                                     unconditional_guidance_scale=scale,
                                                     unconditional_conditioning=un_cond)

        if config.save_memory:
            model.low_vram_shift(is_diffusing=False)

        x_samples = model.decode_first_stage(samples)
        x_samples = (einops.rearrange(x_samples, 'b c h w -> b h w c')
                     * 127.5 + 127.5).cpu().numpy().clip(0, 255).astype(numpy.uint8)

        results = [x_samples[i] for i in range(num_samples)]
    return [detected_map] + results


# Make all images from root_directory to save_directory
def make_images(root_directory, save_directory, possible_extensions):
    possible_text_extensions = possible_extensions[0]
    possible_image_extensions = possible_extensions[1]
    for root, dirs, files in os.walk(root_directory):
        if len(files) > 0:
            image_path_list = []
            # Load only image, text files
            for file_name in files:
                if os.path.splitext(file_name)[1] in possible_text_extensions:
                    text_path = os.path.join(root, file_name)
                if os.path.splitext(file_name)[1] in possible_image_extensions:
                    image_path = os.path.join(root, file_name)
                    image_path_list.append(image_path)

            # Make image and save
            for image_path in image_path_list:
                text_file = open(text_path, 'r')
                prompt = text_file.read()
                input_image = cv2.imread(image_path, cv2.IMREAD_COLOR)
                parameters = [det, input_image, prompt, a_prompt, n_prompt, num_samples, image_resolution,
                              detect_resolution, ddim_steps, guess_mode, strength, scale, seed, eta]
                return_image = process(*parameters)
                save_path = image_path.replace(root_directory, save_directory)
                directory = os.path.sep.join(save_path.split(os.path.sep)[:-1])
                if not os.path.isdir(directory):
                    os.makedirs(directory)
                cv2.imwrite(save_path, return_image[1])


root_directory = './preprossed_data'  # your directory
save_directory = './test_data'  # New directory to save


possible_extensions = [['.txt', 'TXT'], ['.jpg', '.jpeg', '.JPG', '.bmp', '.png']]

# Default value from ControlNet
det = 'None'
a_prompt = 'best quality, extremely detailed'
n_prompt = 'longbody, lowres, bad anatomy, bad hands, missing fingers,\
    extra digit, fewer digits, cropped, worst quality, low quality'
num_samples = 1
image_resolution = 512
detect_resolution = 512
ddim_steps = 20
guess_mode = False
strength = 1.0
scale = 9.0
seed = 7777
eta = 0.0

make_images(root_directory, possible_extensions)


def read_text(file):
    with open(file.name, encoding="utf-8") as f:
        content = f.read()
    return content


# gradio for webUI
block = gr.Blocks().queue()
with block:
    with gr.Row():
        gr.Markdown("## Control Stable Diffusion with Synthesized Scribble")
    with gr.Row():
        with gr.Column():
            input_text = gr.File(label="Input text file")
            prompt = gr.Textbox(label="Prompt")
            input_text.change(fn=read_text, inputs=[input_text], outputs=[prompt])
            input_image = gr.Image(source='upload', type="numpy")
            run_button = gr.Button(label="Run")
            num_samples = gr.Slider(label="Images", minimum=1, maximum=12, value=1, step=1)
            seed = gr.Slider(label="Seed", minimum=-1, maximum=2147483647, step=1, value=12345)
            det = gr.Radio(choices=["Scribble_HED", "Scribble_PIDI", "None"],
                           type="value", value="Scribble_HED", label="Preprocessor")
            with gr.Accordion("Advanced options", open=False):
                image_resolution = gr.Slider(label="Image Resolution", minimum=256, maximum=768, value=512, step=64)
                strength = gr.Slider(label="Control Strength", minimum=0.0, maximum=2.0, value=1.0, step=0.01)
                guess_mode = gr.Checkbox(label='Guess Mode', value=False)
                detect_resolution = gr.Slider(label="Preprocessor Resolution",
                                              minimum=128, maximum=1024, value=512, step=1)
                ddim_steps = gr.Slider(label="Steps", minimum=1, maximum=100, value=20, step=1)
                scale = gr.Slider(label="Guidance Scale", minimum=0.1, maximum=30.0, value=9.0, step=0.1)
                eta = gr.Slider(label="DDIM ETA", minimum=0.0, maximum=1.0, value=1.0, step=0.01)
                a_prompt = gr.Textbox(label="Added Prompt", value='best quality')
                n_prompt = gr.Textbox(label="Negative Prompt",
                                      value='lowres, bad anatomy, bad hands, cropped, worst quality')
        with gr.Column():
            result_gallery = gr.Gallery(label='Output', show_label=False,
                                        elem_id="gallery").style(grid=2, height='auto')
    ips = [det, input_image, prompt, a_prompt, n_prompt, num_samples, image_resolution,
           detect_resolution, ddim_steps, guess_mode, strength, scale, seed, eta]
    run_button.click(fn=process, inputs=ips, outputs=[result_gallery])


block.launch(server_name='0.0.0.0')
