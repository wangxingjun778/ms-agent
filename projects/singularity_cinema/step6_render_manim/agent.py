# Copyright (c) Alibaba, Inc. and its affiliates.
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Union

import json
from moviepy import VideoFileClip
from ms_agent.agent import CodeAgent
from ms_agent.llm import LLM, Message
from ms_agent.utils import get_logger
from omegaconf import DictConfig

logger = get_logger()


class RenderManim(CodeAgent):

    window_size = (1800, 900)

    def __init__(self,
                 config: DictConfig,
                 tag: str,
                 trust_remote_code: bool = False,
                 **kwargs):
        super().__init__(config, tag, trust_remote_code, **kwargs)
        self.work_dir = getattr(self.config, 'output_dir', 'output')
        self.num_parallel = getattr(self.config, 'llm_num_parallel', 10)
        self.manim_render_timeout = getattr(self.config,
                                            'manim_render_timeout', 300)
        self.render_dir = os.path.join(self.work_dir, 'manim_render')
        os.makedirs(self.render_dir, exist_ok=True)

    async def execute_code(self, messages: Union[str, List[Message]],
                           **kwargs) -> List[Message]:
        with open(os.path.join(self.work_dir, 'segments.txt'), 'r') as f:
            segments = json.load(f)
        manim_code_dir = os.path.join(self.work_dir, 'manim_code')
        manim_code = []
        for i in range(len(segments)):
            with open(os.path.join(manim_code_dir, f'segment_{i+1}.py'),
                      'r') as f:
                manim_code.append(f.read())
        with open(os.path.join(self.work_dir, 'audio_info.txt'), 'r') as f:
            audio_infos = json.load(f)
        assert len(manim_code) == len(segments)
        logger.info('Rendering manim code.')

        tasks = [
            (i, segment, code, audio_info['audio_duration'])
            for i, (segment, code, audio_info
                    ) in enumerate(zip(segments, manim_code, audio_infos))
        ]

        with ThreadPoolExecutor(max_workers=self.num_parallel) as executor:
            futures = {
                executor.submit(self._render_manim_scene_static, i, segment,
                                code, duration, self.config, self.work_dir,
                                self.render_dir, self.window_size,
                                self.manim_render_timeout): i
                for i, segment, code, duration in tasks
            }
            for future in as_completed(futures):
                future.result()  # Wait for completion and raise any exceptions

        return messages

    @staticmethod
    def _render_manim_scene_static(i, segment, code, audio_duration, config,
                                   work_dir, render_dir, window_size,
                                   manim_render_timeout):
        """Static method for multiprocessing"""
        llm = LLM.from_config(config)
        return RenderManim._render_manim_impl(llm, i, segment, code,
                                              audio_duration, work_dir,
                                              render_dir, window_size,
                                              manim_render_timeout)

    @staticmethod
    def _render_manim_impl(llm, i, segment, code, audio_duration, work_dir,
                           render_dir, window_size, manim_render_timeout):
        scene_name = f'Scene{i+1}'  # sometimes actual_scene_name cannot find matched class, so do not change this name
        logger.info(f'Rendering manim code for: scene_{i + 1}')
        output_dir = os.path.join(render_dir, f'scene_{i + 1}')
        os.makedirs(output_dir, exist_ok=True)
        if 'manim' not in segment:
            return None
        code_file = os.path.join(output_dir, f'{scene_name}.py')
        class_match = re.search(r'class\s+(\w+)\s*\(Scene\)', code)
        actual_scene_name = class_match.group(1) if class_match else scene_name
        output_path = os.path.join(output_dir, f'{scene_name}.mov')
        manim_requirement = segment.get('manim')
        class_name = f'Scene{i + 1}'
        content = segment['content']
        final_file_path = None
        if os.path.exists(output_path):
            return output_path
        logger.info(f'Rendering scene {actual_scene_name}')
        fix_history = ''
        for retry_idx in range(5):
            with open(code_file, 'w') as f:
                f.write(code)

            env = os.environ.copy()
            env['PYTHONWARNINGS'] = 'ignore'
            env['MANIM_DISABLE_OPENCACHING'] = '1'
            env['PYTHONIOENCODING'] = 'utf-8'
            env['LANG'] = 'zh_CN.UTF-8'
            env['LC_ALL'] = 'zh_CN.UTF-8'
            window_size_str = ','.join([str(x) for x in window_size])
            cmd = [
                'manim', 'render', '-ql', '--transparent', '--format=mov',
                f'--resolution={window_size_str}', '--disable_caching',
                os.path.basename(code_file), actual_scene_name
            ]

            try:
                process = subprocess.Popen(
                    cmd,
                    cwd=output_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='ignore',
                    env=env)

                # Wait for process to complete with timeout
                stdout, stderr = process.communicate(
                    timeout=manim_render_timeout)

                # Create result object compatible with original logic
                class Result:

                    def __init__(self, returncode, stdout, stderr):
                        self.returncode = returncode
                        self.stdout = stdout
                        self.stderr = stderr

                result = Result(process.returncode, stdout, stderr)
                output_text = (result.stdout or '') + (result.stderr or '')
            except subprocess.TimeoutExpired as e:
                output_text = (e.stdout.decode('utf-8', errors='ignore')
                               if e.stdout else '') + (
                                   e.stderr.decode('utf-8', errors='ignore')
                                   if e.stderr else '')  # noqa
                logger.error(
                    f'Manim rendering timed out after {manim_render_timeout} '
                    f'seconds for {actual_scene_name}, output: {output_text}')
                logger.info('Trying to fix manim code.')
                code, fix_history = RenderManim._fix_manim_code_impl(
                    llm, output_text, fix_history, code, manim_requirement,
                    class_name, content, audio_duration)
                continue

            if result.returncode != 0:
                logger.warning(
                    f'Manim command exited with code {result.returncode}')
                logger.warning(f'Output: {output_text}')

                real_error_indicators = [
                    'SyntaxError', 'NameError', 'ImportError',
                    'AttributeError', 'TypeError', 'ValueError',
                    'ModuleNotFoundError', 'Traceback', 'Error:',
                    'Failed to render', 'unexpected keyword argument',
                    'got an unexpected', 'invalid syntax'
                ]

                if any([
                        error_indicator in output_text
                        for error_indicator in real_error_indicators
                ]):
                    logger.info('Trying to fix manim code.')
                    code, fix_history = RenderManim._fix_manim_code_impl(
                        llm, output_text, fix_history, code, manim_requirement,
                        class_name, content, audio_duration)
                    continue

            for root, dirs, files in os.walk(output_dir):
                for file in files:
                    if file == f'{actual_scene_name}.mov':
                        found_file = os.path.join(root, file)
                        if not RenderManim.verify_and_fix_mov_file(found_file):
                            fixed_path = RenderManim.convert_mov_to_compatible(
                                found_file)
                            if fixed_path:
                                found_file = fixed_path

                        shutil.copy2(found_file, output_path)
                        scaled_path = RenderManim.scale_video_to_fit(
                            output_path, target_size=window_size)
                        if scaled_path and scaled_path != output_path:
                            shutil.rmtree(output_path, ignore_errors=True)
                            shutil.copy2(scaled_path, output_path)
                        final_file_path = output_path
            if not final_file_path:
                logger.error(
                    f'Manim file: {class_name} not found, trying to fix manim code.'
                )
                code, fix_history = RenderManim._fix_manim_code_impl(
                    llm, output_text, fix_history, code, manim_requirement,
                    class_name, content, audio_duration)
            else:
                break
        if final_file_path:
            manim_code_dir = os.path.join(work_dir, 'manim_code')
            manim_file = os.path.join(manim_code_dir, f'segment_{i + 1}.py')
            with open(manim_file, 'w') as f:
                f.write(code)
        else:
            raise FileNotFoundError(final_file_path)

    @staticmethod
    def _fix_manim_code_impl(llm, error_log, fix_history, manim_code,
                             manim_requirement, class_name, content,
                             audio_duration):
        fix_request = f"""You are a professional code debugging specialist. You need to help me fix issues in the code. Error messages will be passed directly to you. You need to carefully examine the problems and provide the correct, complete code.
{error_log}

**Original Code**:
```python
{manim_code}
```

{fix_history}

**Original code task**: Create manim animation
- Class name: {class_name}
- Content: {content}
- Extra requirement: {manim_requirement}
- Duration: {audio_duration} seconds
- Code language: **Python**

- Please focus on solving the detected issues
- If you find other issues, fix them too
- Keep the good parts, do minimum changes, only fix problematic areas
- Ensure no new layout issues are introduced
- If some issues are difficult to solve, prioritize the most impactful ones
- There may be some beginner's error because the code was generated by an AI model

Please precisely fix the detected issues while maintaining the richness and creativity of the animation.
""" # noqa
        inputs = [Message(role='user', content=fix_request)]
        _response_message = llm.generate(inputs)
        response = _response_message.content
        if '```python' in response:
            manim_code = response.split('```python')[1].split('```')[0]
        elif '```' in response:
            manim_code = response.split('```')[1].split('```')[0]
        else:
            manim_code = response
        fix_history = (
            f'You have a fix history which generates the code which is given to you:\n\n{fix_request}\n\n'
            f'If last error is same with latest error, **You probably find a wrong root cause**, '
            f'Check carefully and fix it again.**')
        return manim_code, fix_history

    @staticmethod
    def verify_and_fix_mov_file(mov_path):
        clip = VideoFileClip(mov_path)
        frame = clip.get_frame(0)
        clip.close()
        return frame is not None

    @staticmethod
    def convert_mov_to_compatible(mov_path):
        base_path, ext = os.path.splitext(mov_path)
        fixed_path = f'{base_path}_fixed.mov'
        clip = VideoFileClip(mov_path)
        clip.write_videofile(
            fixed_path,
            codec='libx264',
            audio_codec='aac' if clip.audio else None,
            fps=24,
            verbose=False,
            logger=None,
            ffmpeg_params=['-pix_fmt', 'yuva420p'])

        clip.close()
        if RenderManim.verify_and_fix_mov_file(fixed_path):
            return fixed_path
        else:
            return None

    @staticmethod
    def scale_video_to_fit(video_path, target_size=None):
        if target_size is None:
            target_size = RenderManim.window_size
        if not os.path.exists(video_path):
            return video_path

        clip = VideoFileClip(video_path)
        original_size = clip.size

        target_width, target_height = target_size
        original_width, original_height = original_size

        scale_x = target_width / original_width
        scale_y = target_height / original_height
        scale_factor = min(scale_x, scale_y, 1.0)

        if scale_factor < 0.95:
            scaled_clip = clip.resized(scale_factor)

            base_path, ext = os.path.splitext(video_path)
            scaled_path = f'{base_path}_scaled{ext}'
            scaled_clip.write_videofile(
                scaled_path,
                codec='libx264',
                audio_codec='aac' if scaled_clip.audio else None,
                fps=24,
                verbose=False,
                logger=None)

            clip.close()
            scaled_clip.close()
            return scaled_path
        else:
            return video_path
