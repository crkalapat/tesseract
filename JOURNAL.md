---
title: "Tesseract"
author: "crkalapat"
description: "A cuboid smart assistant"
created_at: "2026-06-17"
---

**Total time spent on project: 16.41h**

_Note: All journal entries from June 24 - 28 were retroactively added from Hack Club's Stardance Platform_

## June 30th: PCB Placement

I managed to finalize the layout of the PCB today, and the schematic as well. This took longer than I expected, as I had to
constantly go back and forth tweaking both the cutouts of the top of Tesseract in CAD, and tweaking the size of the top strips
in order for everything to fit on the PCB. My original design had the buttons too close to the edge of the cube case, which
essentially required me to have the buttons less than 0.3 mm away from the edge of the PCB. I also changed the middle strip cutout
to fit in a 2" x 2" acrylic square, which I found a sample online for $1. It took a few tries, but eventually I just succumbed to
making the PCB take up as much of the available space on the top (which is why it now looks like a square). I collected dimensions
from CAD to move the microphones and buttons exactly where they need to be in KiCad. Finally, I wanted to add silkscreen text so I
know which wire goes to which wire during assembly, so I started on that before eventually calling it a day. Once the silkscreen pin labels are done though, it's off to routing!

![Updated PCB Image](assets/pcb-ss-2.png)

**Total time spent: 3h**

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

![5th CAD Picture](assets/cad-ss-4.png)

**Total time spent: 5h**

## June 27th: Designing the PCB

After a (very tragic) discovery that basically nobody online sells 2x9 or 2x10 JST connectors, I updated my schematic to use 2 1x10
or 1x9 JST XH connectors for the connections to the OLED display and RPI Zero 2W. I found footprints for all my symbols, finished
the schematic, and started the PCB layout process. Turns out that JST sockets are pretty big (relatively speaking). I did some layout
work, and now I have a rough sense of where everything will go (see pic below), but I still need to finalize all the positions in CAD
so everything is precise and lines up. Although I wanted to keep the PCB as small as I could, something tells me that I might have
to make it bigger to allow for more room and spacing for components.

![1st PCB Picture](assets/pcb-ss-1.png)

**Total time spent: 1.73h**

## June 26th: Schematic Time

Decided to take a break from the CAD to focus on designing a PCB that will serve as a middle man between the Pi and the other
electronics. I finished most of the schematic (still need to add LEDs), and had to do a fair share of datasheet reading in order to
figure out what to wire to what. Right now the schematic has a speaker IC, two microphones, a display connector, a giant connector to
the Raspberry Pi (which I plan to separate at the endpoint into individual wires), 4 push buttons, and lots of decoupling capacitors.
I haven’t decided fully on footprints for some symbols (like the connectors), but I’m leaning right now towards JST connectors so
that the inside of the cube is not a messy wire spaghetti. _ Fingers crossed _ this schematic doesn’t lead to me making a broken PCB.

![4th CAD Picture](assets/schem-ss-1.png)

**Total time spent: 1.68h**

## June 25th: Cutouts

Moved the Raspberry Pi Zero 2W from being mounted on the ceiling to being mounted with standoffs above the speaker. In the process,
I also modified the speaker mount that I made last time to use threaded inserts so the case could be 3D printed easier. After this,
I wanted the cube to look better, so I added some cutouts on the sides for LEDs, buttons, and for the microphone. Finally, I ended
this session by rounding all the corners ever so slightly with a 1mm fillet, so it won’t be as sharp to touch.

![3rd CAD Picture](assets/cad-ss-3.png)

**Total time spent: 1.67h**

## June 24th: Adding the Speaker

I realized that I needed space to have rubber feet (so that the downward firing speaker’s audio isn’t muffled), so I had to redesign
the speaker grill to be smaller. I added in some rubber feet once that was done and there was room. In the process, I also decided
to use the rubber feet’s screws as a way to screw the display cover and the rest of the enclosure. Finally, I went on Mouser and
found a pretty good fitting speaker for the case, and I mounted it inside the cube with some M3 screws and nuts.

![2nd CAD Picture](assets/cad-ss-2.png)

**Total time spent: 1.83h**

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
