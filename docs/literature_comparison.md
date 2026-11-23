# Abgleich mit Forschung und amtlichen Studien

Stand Nov 2026. Die Zahlen stammen aus Suchmaschinen-Auszügen der genannten Dokumente; viele Primärquellen (DB InfraGO,
Digitale Schiene, ERA, arXiv) waren aus der Rechenumgebung nicht direkt abrufbar. Vor dem Zitieren am Original prüfen.

| # | Modellbefund | Literatur | Urteil |
|---|---|---|---|
| 1 | ETCS L2 **mit** Signalen: Zugfolge 3 → 2 min für ausgerüstete Züge, sehr gutes Kosten-Nutzen-Verhältnis | DB/Digitaler Knoten Stuttgart (2021): ≈ 20 % kürzere Mindestzugfolge, und das mit ETCS + ATO; McKinsey/BMVI (2018) 20–35 % (nur Sekundärquelle); BT-Drs. 19/14721: „höchstens ~20 %“. DB InfraGO ETCS-Ausrüstungsstrategie (2024): Kapazitätsgewinn kommt aus L2 **ohne** Signale; bei 1:1-Überlagerung eher Kapazitätsverlust | **teilweise widersprochen**: Richtung ja, Größe zu optimistisch (−33 % statt ≈ −20 %) |
| 2 | Nur-ETCS ist wegen der Fahrzeugnachrüstung deutlich schlechter pro Euro | ERA (2022): Nachrüstung 0,45 → 0,9 Mio € je Fahrzeug (2018 → 2022); Dänemark ≈ 1,3 Mio €/km gesamt; Bund ≈ 1,7 Mrd € für Fahrzeugausrüstung bis 2030 | **gestützt**; Modellkosten je Fahrzeug (0,4–0,6 Mio €) eher zu niedrig |
| 3 | Engpässe: Ruhr-Hauptstrecke, Hamburg Hbf, Frankfurt | DB InfraGO: Rhein-Ruhr-Achse Köln-Mülheim–Duisburg–Dortmund als „überlastet“ erklärt (PEK 2022); Hamburg Hbf überlastet (PEK Hamburg, Verbindungsbahn 20–26 Züge/h); Riedbahn/Main-Neckar-Bahn 2040 bei 127 % / 121 % Auslastung | **gestützt** |
| 4 | Heiße Reserven an großen Knoten sind günstig und wirksam | Hoogervorst et al., Transp. Sci. 2020 (flexibler Fahrzeugeinsatz senkt Fahrgastverspätung); CSH 2023 (arXiv 2310.13773): Kaskaden v. a. über Fahrzeugumläufe; Fahrzeugstörungen 13 % der FV-Verspätungen (2019) | **neutral bis schwach gestützt**, keine direkte Kostenstudie |
| 5 | Fahrplan aufweichen (≤ 3 min) bringt kaum etwas | Kroon et al., TR-B 2008; Vromans 2005: kleine Umverteilungen von Puffern senken mittlere Verspätung „oft deutlich“ | **teilweise widersprochen**; Modell testet nur eine eingeschränkte Variante |
| 6 | 95 % sind mit Infrastruktur allein nicht erreichbar, Primärverspätungen dominieren | DB 2023: nur ≈ 18 % direkt infrastrukturbedingt, aber 61 % der Überlastung zugeschrieben; BEG: ≈ 60 % Folgeverspätungen, Haltezeitüberschreitung 9–12 %, Fahrzeug 6 %; SBB 92–94 % (3-min-Schwelle) | **gemischt**, stark abhängig von der Zuordnung |
| 7 | Große Neubaustrecken haben wenig Verspätungsnutzen pro Euro | NKV Frankfurt–Mannheim 1,1 (2026), Hannover–Bielefeld 1,04 (Gutachter-Vorstudie), Alpha-E-Beirat: Hamburg–Hannover realistisch 0,9 | **gestützt** (NKV misst Reisezeit/Verlagerung, nicht Pünktlichkeit) |
| 8 | Validierung: Bahnhöfe r ≈ 0,83, Verspätungszuwachs je Abschnitt r ≈ 0,55 | Keine direkt vergleichbaren r-Werte publiziert (van der Meer/Goverde 2010, Schwab et al. WSC 2024, Corman & Kecman 2018 berichten MAE oder qualitative Güte) | **neutral**; Bahnhofsebene gut, Abschnittsebene Schwachpunkt |

## Konsequenzen für die Ergebnisse

* **Größtes Risiko: ETCS mit Signalen.** Die Spitzenplätze (ETCS Essen–Duisburg, Hamm–Dortmund) hängen an der
  Annahme 3 → 2 min. Realistischer sind ≈ 2,4–2,5 min, und ohne Blockverdichtung bei Überlagerung womöglich kaum
  Gewinn. Ein Lauf mit `GR_HEADWAY_ETCS=2.5` (früheres Modell: Nutzen −33 %, Rang blieb vorn) und eine Variante
  „L2 mit Signalen ohne Zugfolgegewinn“ sind nötig, bevor die Rangfolge belastbar ist.
* **Nur-ETCS:** bei 0,9 Mio € je Fahrzeug noch schlechter – der Befund wird stärker.
* **Engpassidentifikation** stimmt mit den amtlichen Überlastungserklärungen überein.
* **Fahrplan:** optimierte Pufferumverteilung (statt reiner Verschiebung ≤ 3 min) verdient einen eigenen Test.
