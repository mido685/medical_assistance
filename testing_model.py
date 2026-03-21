import re
from transformers import AutoTokenizer, AutoModelForTokenClassification, pipeline
from pathlib import Path

path = Path("/mnt/c/Users/HP/OneDrive/Desktop/medical_assistance/checkpoint-3270")

tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
model = AutoModelForTokenClassification.from_pretrained(path, local_files_only=True)

ner = pipeline("ner", model=model, tokenizer=tokenizer, aggregation_strategy="first")

# All frequency patterns from your dataset
FREQ_PATTERNS = re.compile(
    r'\b(every\s+\d+\s+hours?|every\s+morning|every\s+evening|every\s+night|'
    r'once\s+daily|once\s+at\s+night|twice\s+a\s+day|twice\s+daily|'
    r'three\s+times\s+daily|three\s+times\s+a\s+day|'
    r'four\s+times\s+daily|four\s+times\s+a\s+day|\d+\s+times\s+daily)\b',
    re.IGNORECASE
)

def predict(text):
    results = ner(text)

    # Override TIME→FREQ when it matches frequency pattern
    for entity in results:
        if entity["entity_group"] == "TIME":
            if FREQ_PATTERNS.search(entity["word"]):
                entity["entity_group"] = "FREQ"

    # Also catch any FREQ missed entirely by the model
    detected_spans = [(e["start"], e["end"]) for e in results]
    for match in FREQ_PATTERNS.finditer(text):
        already_detected = any(s <= match.start() < e for s, e in detected_spans)
        if not already_detected:
            results.append({
                "entity_group": "FREQ",
                "word": match.group(),
                "start": match.start(),
                "end": match.end(),
                "score": 1.0
            })

    return sorted(results, key=lambda x: x["start"])

# Test
texts = [
 'I take Aspirin 500mg twice daily at 8:00 pm',
]

for text in texts:
    print(f"\nInput: {text}")
    for e in predict(text):
        print(f"  {e['entity_group']:<12} | {e['word']:<25} | score: {e['score']:.4f}")