# BosTix: The background
One night, I was scrolling on X and saw this super cool [real-time parking ticket map](https://walzr.com/sf-parking/about/) for San Francisco, built by Riley Walz. Immediately, I knew I had to do the same thing for Boston; after all, ticketing is just as pervasive and notorious here. 
That said, my goal was (seemingly) simple: find a way to retrieve tickets chronologically.

I quickly found the official [payment portal for tickets](https://bostonma.rmcpay.com/). This portal lets you to search by ticket number, plate number, or VIN--problem is, I don't have any tickets! Lucky for me, the plate `123456` had a few. I poked around with the site's features and found a few interesting API endpoints. 

## Research
### Ticket Search
The `/searchviolation` endpoint powers searches. It returns a JSON response with a `data` array which is either empty if no tickets exist, or contains every ticket associated with that plate otherwise. Each ticket has a LOT of JSON fields. Some even show up as "userdefX", where there is another JSON field "userdefX_label" defining what it holds! Cool approach.
For my approach, I needed to focus on identifiers. Each ticket has two main ones--`id` and `violation_number`. When I used other ticket API endpoints like the print option, they used `id`, but search used the `violation_number`.

![Search Pic](https://github.com/jack898/PollingAuto/blob/main/searchviolationEx.jpg?raw=true)

I tried just iterating `violation_number` directly, but this didn't work. Most numbers had no tickets at all, and when I did find one sometimes the ticket had a higher number but an earlier issue date!

### New Approach
Since the violation number didn't work, I tested whether the id parameter was chronological using `/getviolationreceipt` (ticket printing). Ultimately to retrieve the ticket information I'd still need the violation number, but conveniently the generated PDF displayed this value. As I iterated the violationid, though, I noticed something strange--tickets from other cities appeared!

![Other city ticket](https://github.com/jack898/PollingAuto/blob/main/MDticketprint.jpg?raw=true)
![Other city ticket2](https://github.com/jack898/PollingAuto/blob/main/PAticketprint.jpg?raw=true)

I realized that many cities actually use a city.rmcpay.com portal. RMCPay seems to be a ticket payment system by [Passport Inc](https://www.passportinc.com/), and I guess a lot of cities share the same backend ticket database.
This was bad news for me: Boston tickets were interspersed with IDs from other cities, creating lots of gaps. Still, the Boston ids looked *roughly* chronological.

### Number Patterns
I checked for correlation between `id` and `violation_number`. Interestingly for any Boston tickets from 2024 onward, `violation_number - id = 740727581`.
Older years used different offsets, with occasional outliers. My hypothesis is that Boston needed their own numbering system and just chooses a predefined offset, depending on the device issuing the ticket. Possibly they made the issuing devices uniform in 2024.

This explained the gaps in `violation_number`--Boston's numbers come from the RMCPay IDs, with many IDs belonging to other cities.

I also noticed clustering; tickets would appear in batches, presumably because officers submitted them in groups. That also explains how some larger IDs map to earlier issuing dates.

Overall, I was able to conclude that the violation numbers were semi-chronological, with a bit of noise. Now I had to build a scraper.

## Scraper
### Initial Development
Scraping the tickets posed a few challenges:
1. Violation numbers are not fully chronological
2. Large, irregularly sized gaps appear due to other cities' tickets
3. Rate limiting :(

To capture tickets in real-time, I needed to stay close to the most recent ID without just bruteforcing a million numbers.
I tried a lot of different tricks and ideas here by making a Python scraping script, setting up a job on GitHub actions, and tracking the tickets for a couple days. 

### Rollback Approach
My first approach was a simple scraper that iterated the violation numbers, and if it went 15000 numbers without getting a ticket, it would roll back to the starting violation number. This worked at first, but /searchviolation endpoint is not super fast to respond, plus I have to do some JSON parsing, so it wasn't practical to re-scan 15000 tickets repeatedly. 
I couldn't have each job take 30+ minutes.

### Rollback + smaller scans
A better approach involved a little bit more accounting:
1. Scan only 1000 tickets on each job (down from 20k), to reduce runtime
2. Instead of using the starting violation number, we keep track of the largest violation number. We revert to THAT violation number if we go 10000 tickets with no findings, and we also start our next job from this number.
3. Rescan each range multiple times to find clusters
4. We track the violation numbers seen to deduplicate before putting tickets in the CSV

### + Date tracking
One issue with tracking the largest violation number is that the tickets are not always chronological--with that, I tried tracking the latest date instead. This worked a little bit better!

### Final Approach
We keep a few txt files to persist data across jobs. "VID" here refers to violation number:
- `last_vid.txt`: Current scanning position (which VID to start from next)
- `last_valid_vid.txt`: The VID of the most recent valid ticket 
   `last_date.txt`: Date of the most recent ticket found
- `pass_count.txt`: How many times the current range has been scanned (0-2)
- `gap_count.txt`: Counter for consecutive empty responses (resets at 10000)
- `seen_vids.txt`: All VIDs already processed, to avoid duplicate entries

The overall strategy is; we scan 1000 VIDs, from `last_vid` to `last_vid+1000`. We scan each range 3 times before advancing 3000 tickets forward if nothing is found. If we find a newer ticket, this becomes our next starting point. If we get through 10,000 consecutive VIDs with no tickets found, we assume we've gone too far and revert to `last_valid_vid`.

I ran this approach for about a week and it stayed "good enough". Admittedly, it sometimes struggles to catch up during off times when IDs jumped quickly, but it usually found tickets issued within a few hours, and often many within the past hour during the day.

## Website
I wanted a sleek UI like Riley's, but frontend development is not my strength. This is where AI came in handy! I used Bolt to build the UI and set up edge functions to keep the site updated.
Building with AI taught me some lessons:
1. You need to have *some* understanding of the stack, or deugging is hopeless. Models often make silly mistakes, and end up stuck if you ask them to debug.
2. Even "end-to-end" AI tools struggle with backend integrations. I learned a lot about Supabase and Github Actions fixing things that Bolt broke.

Still, I concluded that if you have a basic understanding of fundamentals, and access to a strong LLM, you can build pretty impressive projects quickly!

# Results
I (and Bolt) built an interactive map interface to view Boston parking tickets in somewhat real time. In addition, I have gathered a LOT of data on Boston parking tickets.

Just a couple interesting observations I've had so far:
1. They ticket all night! I assumed "overnight" tickets came from early morning/night patrols, but I saw tickets regularly issued from 1-4am.
2. Also contrary to popular belief, they ticket on Sundays! I originally designed the site to not even show Sundays, but I tested it and saw quite a few Expired Inspection/Registration Plate tickets, and even a few "Resident Permit Only" tickets.
3. Overall, "Meter Fee Unpaid" and "Resident Permit Only" violations dominate, except on Sundays when the Expired Inspection or Expired Registration plate tickets are more common.

# Disclaimer
I am not a lawyer, data scientist, or expert software engineer. There is probably (definitely) a better approach for everything I did, and I encourage anybody to improve upon it.
All data comes from public records. This tool is for educational and research purposes only. I am not affiliated with the City of Boston or RMCpay.

Use responsibly, respect parking laws, and enjoy.

