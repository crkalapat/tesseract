---
title: "Tesseract"
author: "crkalapat"
description: "A cuboid smart assistant"
created_at: "2025-06-17"
---

**Total time spent on project: 6.5h**

### June 29th: Redesign, redesign

I worked into the wee hours of midnight this morning and yesterday afternoon to fix a bunch of small things and add a few small
things to the project. There's a lot of changes (since it was around 5h worth of work), so here's a quick list of everything that
changed from the last devlog:

- Changed speaker grills to be alternating so that they would allow for more airflow
- Added in SparkFun USB C Horizontal Connector breakout board, and a slot and screws for it in the case
- Designed a speaker enclosure from scratch so that the speaker can sound better and be sealed
- Redesigned the top cutout design to accomodate for a larger light strip, larger buttons, and mic grills
- Made the side grills not penetrate the enclosure, so you can't see inside and the design is more consistent overall
- Fixed everything else to still fit in the enclosure (ie getting larger screws, moving around mounting points, making cutout slots)

Out of all of the above bullet points, the speaker enclosure in particular took quite a bit of time, as I tried to make it as big
as practically possible without it stealing space for other components. The top cutout design also took a little bit of trial and
error, as I experimented with different strip lengths, spacings, and button sizes. I also couldn't find a CAD model for the longest
time for the SparkFun USB C breakout board, so I had to open and fix the Eagle files with a correct USB C footprint and export it
to get a STEP file for the assembly. Took way longer than I expected (and probably than it should have), since I really, _really_
wanted to get the placement of the actual USB C receptacle right to make a precise cutout in the enclosure case.

It's slowly starting to get finished!

![Updated CAD Picture](assets/cad-ss-4.png)

**Total time spent: 5h**

## June 17th: Start!

I decided to try and build my own smart speaker for my room, since I was curious about creating a more agentic
AI smart assistant than what I have write now with an Echo Dot. I mostly worked on CAD today (techincally yesterday but
I decided to put my designing into a journal as well as lapsing it). A really important feature of the design that I want
is for my smart assistant to be a cube, and for it to be like an alarm clock with a display on the front. Hence the name,
_Tesseract_ (a cube that has time).

In the CAD specifically, I made some speaker grill holes on a plate in the bottom for the downward firing speaker. I decided
to use a Raspberry Pi Zero 2W for now to serve as a lightweight computer to record audio and send it to cloud APIs. In addition,
I also settled on using a 1.91" OLED display for the actual alarm clock face. I wanted square OLED specifically here so that I
would avoid having a not true black of an LCD blaring in my face at night, and also have the tesseract-y vibe of two squares
nested inside each other. Currently I have a display cover in the CAD that has a slot for the display, and I plan on using
threaded inserts to mount the RPI Zero 2W to the case.

![1st CAD Picture](assets/cad-ss-1.png)

**Total time spent: 1.5h**
