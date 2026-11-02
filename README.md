# Monitor pompa di calore

Cruscotto Streamlit per leggere i dati Modbus di un controllore Dixell iC208 e di un driver IEV22D.
Lo script di lettura non scrive. L'app può scrivere solo i set point, su richiesta esplicita.

## Avvio

```powershell
pip install -r requirements.txt
streamlit run app.py
```

## Lettura dal vivo

Nella barra laterale attiva "Lettura dal vivo" e controlla IP e porta del gateway.
Ogni lettura viene aggiunta a `data/live_AAAAMMGG.csv`. Serve la pagina aperta.
Non lanciare in parallelo `leggi_ichill.py`, per non sovrapporre le richieste sul bus.

## Modifica dei set point

Attiva "Abilita modifica set point" nella barra laterale. L'app può scrivere solo i set point, con conferma, limiti di sicurezza e rilettura.
I limiti sono in `tools/scrivi_set.py`. Le modifiche vengono registrate in `logs/scritture.csv`.

## Nuove letture

```powershell
python tools/leggi_ichill.py 192.168.1.230 --out data/lettura.csv
```

Ogni CSV in `data/` viene mostrato nell'app. Usa un nome diverso per ogni file, così le letture restano nello storico.

## Limiti

- I registri seguono il documento Enerblue DT00216 Rev05 per firmware 4.6-4.8.
- La funzione delle sonde segue la tabella Orange Inverter, da confermare sullo schema della macchina.
- La scala delle ore non è indicata: va confrontata con il display.
- Pubblicata su Streamlit Cloud, l'app non raggiunge la rete di casa: la lettura dal vivo funziona solo in locale.
