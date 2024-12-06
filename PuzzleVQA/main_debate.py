from pathlib import Path

import pandas as pd
from fire import Fire
from pydantic import BaseModel
from tqdm import tqdm

from data_loading import Data, Sample, convert_text_to_image
from modeling import select_model
from prompting import select_prompter

import mad
import base64


class Scorer(BaseModel):
    def run(self, sample: Sample) -> float:
        raise NotImplementedError


class ExactScorer(Scorer):
    def run(self, sample: Sample) -> float:
        if sample.pred == sample.answer:
            return 1.0
        return 0.0

# Function to encode the image
def encode_image(image_path):
  with open(image_path, "rb") as image_file:
    return base64.b64encode(image_file.read()).decode('utf-8')


# Multi agent debate framework (Society of Mind)
def evaluate_multi_choice(
    data_path: str,
    image_dir: str = "data",
    prompt_name: str = "cot_multi_extract",
    output_dir: str = "outputs_debate2",
    prevent_direct_answer: bool = False,
    use_describe_image_prompt: bool = True,
    **kwargs,
):
    print(locals())
    data = Data.load_with_image_dir(data_path, image_dir)  # json의 각 객체
    model_name = kwargs.get("model_name")
    path_out = f"{output_dir}/{Path(data_path).stem}/{model_name}/{prompt_name}.jsonl"
    print(dict(path_out=path_out))

    is_correct = []
    progress = tqdm(data.samples, desc=path_out)
    sample: Sample
    prompter = select_prompter(prompt_name)
    scorer = ExactScorer()
    model = select_model(**kwargs)

    # GPT-4V sometimes becomes very lazy when prompted not to directly give the final answer
    if not prevent_direct_answer:
        prompter.base_prompter.prevent_direct_answer = False
    if not use_describe_image_prompt:
        prompter.base_prompter.use_describe_image_prompt = False

    for sample in progress:
        # bring base prompt
        sample.prompt = prompter.base_prompter.run(sample)
        # print("sample.prompt:\n", sample.prompt)

        # image = convert_text_to_image(sample.image_string)
        image_path = "data/"+sample.image
        base64_image = encode_image(image_path)
        sample.image_string = ""

        num_agents = kwargs.get("num_agents")  # number of agents
        agent_context_prompt = f'''
        {sample.prompt}
        Make sure to state your answer at the end of the response.
        '''
        agent_contexts = [[{"role": "user", "content": [
                {"type": "text",
                 "text": agent_context_prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url":  f"data:image/jpeg;base64,{base64_image}"
                    },
                },
            ]}] for _ in range(num_agents)]
        rounds = kwargs.get("rounds")  # number of rounds
        question_prompt = f'''
        We seek to find the result of {sample.prompt}?
        '''

        for round in range(rounds):
            for i, agent_context in enumerate(agent_contexts):
                if round != 0:
                    other_agents = agent_contexts[:i] + agent_contexts[i + 1:]
                    message = mad.construct_message(other_agents, question_prompt, 2*round - 1)
                    agent_context.append(message)

                completion = mad.generate_answer(agent_context, model_name)

                assistant_message = mad.construct_assistant_message(completion)
                agent_context.append(assistant_message)

        # answer extraction
        answers = []
        for agent_context in agent_contexts:
            text_answer = string =  agent_context[-1]['content']
            agent_answer = f"agent answer: {text_answer}"
            sample.raw_output += agent_answer + "\n"
            print("agent answers:\n", text_answer)

            option_answer = prompter.get_answer(text_answer, sample.options)
            if option_answer is None:
                continue
            answers.append(option_answer)


        # sample.pred = mad.most_frequent([mad.parse_answer(agent_context["content"]) for agent_context in agent_contexts])
        sample.pred = mad.most_frequent(answers)
        print("answers:\n", answers)
        print("sample.pred:\n", sample.pred)

        # scoring
        is_correct.append(scorer.run(sample))
        score = sum(is_correct) / len(is_correct)
        progress.set_postfix(score=score)
        print(sample.json(indent=2, exclude={"image_string"}))
        print(dict(is_correct=is_correct[-1]))
        data.save(path_out)

def print_results(*paths: str):
    scorer = ExactScorer()
    mapping = dict(
        triangle="numbers",
        grid_number="numbers",
        color_grid="colors",
        color_hexagon="colors",
        shape_reflect="shapes",
        shape_morph="shapes",
        size_cycle="size",
        size_grid="size",
        color_number_hexagon="colors-numbers",
        grid_number_color="colors-numbers",
        venn="numbers-shapes",
        polygon_sides_number="numbers-shapes",
        circle_size_number="numbers-size",
        rectangle_height_number="numbers-size",
        polygon_sides_color="colors-shapes",
        color_overlap_squares="colors-shapes",
        rectangle_height_color="colors-size",
        color_size_circle="colors-size",
        shape_size_hexagon="size-shapes",
        shape_size_grid="size-shapes",
    )

    records = {}
    for p in paths:
        task, model, prompt = Path(p).parts[-3:]
        data = Data.load(str(p))
        score = sum(scorer.run(s) for s in data.samples) / len(data.samples) * 100
        if any(s.pred == "" for s in data.samples):
            score = -1

        base = 25 if len(data.samples[0].options) == 4 else 100 / 3
        records.setdefault(task, {}).update(
            concepts=mapping[task], task=task, random_baseline=base
        )
        records[task][model] = score

    print("Individual task results")
    df = pd.DataFrame(list(records.values()))
    df["length"] = df["concepts"].str.len()
    df = df.sort_values(by=["length"]).drop(columns=["length"]).reset_index(drop=True)
    df.loc[len(df)] = df.mean(numeric_only=True)
    print(df.round(1))

    print("Single-concept results")
    df_single = df.dropna()[~df.dropna()["concepts"].str.contains("-")]
    average_row = df_single.mean(numeric_only=True)
    average_row["concepts"] = "~avg."
    df_single.loc[len(df_single)] = average_row
    df_single = df_single.groupby("concepts").mean(numeric_only=True)
    print(df_single.round(1))

    print("Dual-concept results")
    df_single = df.dropna()[df.dropna()["concepts"].str.contains("-")]
    average_row = df_single.mean(numeric_only=True)
    average_row["concepts"] = "~avg."
    df_single.loc[len(df_single)] = average_row
    df_single = df_single.groupby("concepts").mean(numeric_only=True)
    print(df_single.round(1))


"""
python main.py print_results outputs/*/*/*.jsonl #Linux
python main_debate.py print_results (Get-ChildItem outputs_sequential\*\*\*.jsonl).FullName #Window
"""


if __name__ == "__main__":
    Fire()


