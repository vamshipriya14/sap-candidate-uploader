import spacy
nlp = spacy.load("en_core_web_sm")

doc = nlp("Kranthi Hanumanthu")
print([(ent.text, ent.label_) for ent in doc.ents])