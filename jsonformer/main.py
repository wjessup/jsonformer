from typing import List, Union, Dict, Any

from jsonformer.logits_processors import NumberStoppingCriteria, OutputNumbersTokens
from transformers import PreTrainedModel, PreTrainedTokenizer
import json

GENERATION_MARKER = "|GENERATION|"


class Jsonformer:
    value: Dict[str, Any] = {}

    def __init__(
        self,
        model: PreTrainedModel,
        tokenizer: PreTrainedTokenizer,
        json_schema: Dict[str, Any],
        prompt: str,
        *,
        debug: bool = False,
        max_array_length: int = 10,
        max_number_tokens: int = 6,
        temperature: float = 1.0,
        max_string_token_length: int = 10,
    ):
        self.model = model
        self.tokenizer = tokenizer
        self.json_schema = json_schema
        self.prompt = prompt

        self.number_logit_processor = OutputNumbersTokens(self.tokenizer, self.prompt)

        self.generation_marker = "|GENERATION|"
        self.debug_on = debug
        self.max_array_length = max_array_length

        self.max_number_tokens = max_number_tokens
        self.temperature = temperature
        self.max_string_token_length = max_string_token_length

    def debug(self, *args, **kwargs):
        if self.debug_on:
            print(*args, **kwargs)

    def generate_number(self, temperature: Union[float, None] = None, iterations=0):
        prompt = self.get_prompt()
        self.debug("[generate_number] prompt", prompt)
        input_tokens = self.tokenizer.encode(prompt, return_tensors="pt").to(
            self.model.device
        )
        response = self.model.generate(
            input_tokens,
            max_new_tokens=self.max_number_tokens,
            num_return_sequences=1,
            logits_processor=[self.number_logit_processor],
            stopping_criteria=[
                NumberStoppingCriteria(self.tokenizer, len(input_tokens[0]))
            ],
            temperature=temperature or self.temperature,
            pad_token_id=self.tokenizer.eos_token_id,
        )
        response = self.tokenizer.decode(response[0], skip_special_tokens=True)
        self.debug("[generate_number] response", response)
        response = response[len(prompt) :]
        response = response.strip().rstrip(".")

        try:
            return float(response)
        except ValueError:
            if iterations > 3:
                raise ValueError("Failed to generate a valid number")

            return self.generate_number(temperature=self.temperature * 1.3)

    def generate_boolean(self) -> bool:
        prompt = self.get_prompt()
        self.debug("[generate_boolean] prompt", prompt)

        input_tensor = self.tokenizer.encode(prompt, return_tensors="pt")
        output = self.model.forward(input_tensor.to(self.model.device))
        logits = output.logits[0, -1]

        true_token_id = self.tokenizer.convert_tokens_to_ids("true")
        false_token_id = self.tokenizer.convert_tokens_to_ids("false")

        true_logits = logits[true_token_id]
        false_logits = logits[false_token_id]

        if true_logits > false_logits:
            return True
        elif false_logits > true_logits:
            return False
        else:
            print("Failed to generate a valid boolean value")
            return None

    def generate_string(self) -> str:
        prompt = self.get_prompt()
        self.debug("[generate_string] prompt", prompt)
        response = self.model.generate(
            self.tokenizer.encode(prompt, return_tensors="pt").to(self.model.device),
            max_new_tokens=self.max_string_token_length,
            num_return_sequences=1,
            temperature=self.temperature,
            pad_token_id=self.tokenizer.eos_token_id,
        )
        response = self.tokenizer.decode(response[0], skip_special_tokens=True)

        response = response[len(prompt) :].strip()

        self.debug("[generate_string] response", response)
        split = response.split('"')
        assert len(split) >= 2
        return split[1]

    def generate_object(
        self, properties: Dict[str, Any], obj: Dict[str, Any]
    ) -> Dict[str, Any]:
        # self.debug("[generate_object] properties", properties)
        for key, schema in properties.items():
            obj[key] = self.generate_value(schema, obj, key)
        return obj

    def should_generate_non_null(self) -> bool:
        prompt = self.get_prompt()
        input_tensor = self.tokenizer.encode(prompt, return_tensors="pt")
        output = self.model.forward(input_tensor.to(self.model.device))
        logits = output.logits[0, -1]

        # Get the top 5 tokens by logit value and print them
        top_5_tokens = logits.topk(5).indices.tolist()
        print("top 5 tokens")
        for token_id in top_5_tokens:
            decoded_token = self.tokenizer.decode([token_id])
            print(f"|{decoded_token}|: {logits[token_id]}")

        # Sort logits in descending order and loop through them
        sorted_logit_indices = logits.argsort(descending=True).tolist()
        for token_id in sorted_logit_indices:
            decoded_token = self.tokenizer.decode([token_id])

            print(f"decoded_token: |{decoded_token}|")

            decoded_token = decoded_token.strip()

            # If the decoded token is not empty and not only spaces or newlines
            if decoded_token and not decoded_token.isspace():
                if decoded_token == "null":
                    print("decoded_token is null, breaking and returning False")
                    # If the decoded token is "null", break and return False
                    return False
                else:
                    print("decoded_token is not null, returning True")
                    # If the decoded token is not "null", return True
                    return True
            else:
                print("decoded_token is empty or only spaces or newlines - skipping")

        return False

    def generate_value(
        self,
        schema: Dict[str, Any],
        obj: Union[Dict[str, Any], List[Any]],
        key: Union[str, None] = None,
    ) -> Any:
        schema_type = schema["type"]

        if key:
            obj[key] = self.generation_marker
        else:
            obj.append(self.generation_marker)

        if isinstance(schema_type, list):
            if len(schema_type) != 2 or "null" not in schema_type:
                raise ValueError(
                    f"Unsupported schema type: {schema_type}. Only nullable types are supported"
                )

            non_null_type = [t for t in schema_type if t != "null"][0]
            schema_type = non_null_type if self.should_generate_non_null() else "null"

        if schema_type == "null":
            return None

        if schema_type == "number":
            return self.generate_number()
        elif schema_type == "boolean":
            return self.generate_boolean()
        elif schema_type == "string":
            return self.generate_string()
        elif schema_type == "array":
            new_array = []
            if key:
                obj[key] = new_array
            else:
                obj.append(new_array)
            obj[key] = new_array
            return self.generate_array(schema["items"], new_array)
        elif schema_type == "object":
            new_obj = {}
            if key:
                obj[key] = new_obj
            else:
                obj.append(new_obj)
            return self.generate_object(schema["properties"], new_obj)
        else:
            raise ValueError(f"Unsupported schema type: {schema_type}")

    def generate_array(self, item_schema: Dict[str, Any], obj: Dict[str, Any]) -> list:
        for _ in range(self.max_array_length):
            element = self.generate_value(item_schema, obj)
            obj[-1] = element

            obj.append(self.generation_marker)
            input_prompt = self.get_prompt()
            obj.pop()
            input_tensor = self.tokenizer.encode(input_prompt, return_tensors="pt")
            output = self.model.forward(input_tensor.to(self.model.device))
            logits = output.logits[0, -1]

            close_bracket_token_id = self.tokenizer.convert_tokens_to_ids("]")
            comma_token_id = self.tokenizer.convert_tokens_to_ids(", ")
            close_bracket_logits = logits[close_bracket_token_id]
            comma_logits = logits[comma_token_id]

            if close_bracket_logits > comma_logits:
                break

        return obj

    def get_prompt(self):
        template = """{prompt}\nOutput result in the following JSON schema format:\n{schema}\nResult: {progress}"""
        progress = json.dumps(self.value)
        gen_marker_index = progress.find(f'"{self.generation_marker}"')
        if gen_marker_index != -1:
            progress = progress[:gen_marker_index]
        else:
            raise ValueError("Failed to find generation marker")

        prompt = template.format(
            prompt=self.prompt,
            schema=json.dumps(self.json_schema),
            progress=progress,
        )

        return prompt

    def __call__(self) -> Dict[str, Any]:
        self.value = {}
        generated_data = self.generate_object(
            self.json_schema["properties"], self.value
        )
        return generated_data
