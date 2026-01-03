# The goal of this program is to scrape the URL, Title, Text of the reddit story using soup

from bs4 import BeautifulSoup
import requests
import csv

# Potentially could create another scraper to find reddit posts URL and then putting them in here automatically

URL1 = "https://www.reddit.com/r/nosleep/comments/1js3f7c/there_is_a_broken_incubator_in_the_shed_my_wife/"
URL2 = "https://www.reddit.com/r/nosleep/comments/1jo7t8x/my_kidnapper_released_me_two_days_ago/"
URL3 = "https://www.reddit.com/r/nosleep/comments/1jsgnnv/i_was_a_camp_counselor_and_saw_something_i/"
URL4 = "https://www.reddit.com/r/nosleep/comments/1jt3ak9/my_neighbors_kids_wont_stop_knocking_on_my_door/"
URL5 = "https://www.reddit.com/r/nosleep/comments/1jpqv9f/everyone_thinks_i_killed_my_own_brother_but_i/"
URL6 = "https://www.reddit.com/r/nosleep/comments/1jnzkxh/the_pain_in_my_abdomen_keeps_getting_worse/"
URL7 = "https://www.reddit.com/r/nosleep/comments/1jpma7p/how_i_sold_my_soul_to_the_devil/"
URL8 = "https://www.reddit.com/r/nosleep/comments/1johu2y/i_heard_my_dog_barking_outside/"
URL9 = "https://www.reddit.com/r/nosleep/comments/1jvo1xj/we_started_recording_our_fights_to_be_better/"
URL10 = "https://www.reddit.com/r/nosleep/comments/1jvo1xj/we_started_recording_our_fights_to_be_better/"
URL11 = "https://www.reddit.com/r/nosleep/comments/1juh6o0/im_a_nurse_who_broke_protocol_to_save_patients/"
URL12 = "https://www.reddit.com/r/nosleep/comments/1jzlqbb/342_am/"
URL13 = "https://www.reddit.com/r/nosleep/comments/1jupbma/the_job_was_simple_monitor_the_woman_in_room_6/"
URL14 = "https://www.reddit.com/r/nosleep/comments/1kdreds/my_therapist_said_he_keeps_a_copy_of_every/"
URL15 = "https://www.reddit.com/r/nosleep/comments/1kdyyyt/i_bought_a_telescope_to_watch_the_stars_one_of/"
URL16 = "https://www.reddit.com/r/nosleep/comments/1kdlxan/my_brother_believed_he_was_protecting_us_from/"
URL17 = "https://www.reddit.com/r/nosleep/comments/1kacf6m/its_digging_beneath_my_bedroom/"

URLS = [
    "https://www.reddit.com/r/nosleep/comments/1js3f7c/there_is_a_broken_incubator_in_the_shed_my_wife/",
    "https://www.reddit.com/r/nosleep/comments/1jo7t8x/my_kidnapper_released_me_two_days_ago/",
    "https://www.reddit.com/r/nosleep/comments/1jsgnnv/i_was_a_camp_counselor_and_saw_something_i/"
]

try:
    source = requests.get(URL17)
    source.raise_for_status()

    soup = BeautifulSoup(source.text,'html.parser')

    h1_tag = soup.find('h1', class_="font-semibold")

    # Get the aria-label attribute
    aria_label = h1_tag['aria-label']   
    
    #.find_all("aria-label")
    Title = aria_label.replace('Post Title: ', '')

    print(Title)

    # Find the first div that contains at least one <p> tag
    target_div = None
    for div in soup.find_all('div'):
        if div.find('p'):
            target_div = div
            break

    # If found, extract the paragraphs
    if target_div:
        paragraphs = target_div.find_all('p')
        paragraph_texts = [p.get_text(strip=True) for p in paragraphs]
        paragraph_texts.pop()
        paragraph_texts.pop()
        paragraph_texts.remove("PLEASE READ OUR GUIDELINES FIRST. Nosleep is a place for redditors to share their scary personal experiences.")
        Text = '\n\n'.join(paragraph_texts)
        
        print(Text)
    else:
        print("No suitable div found")
    
    full_text = Title + "\n\n" + Text
    max_chars = 9999

    # Split the text into chunks of max_chars
    chunks = [full_text[i:i+max_chars] for i in range(0, len(full_text), max_chars)]

    # Write each chunk to a separate file
    for idx, chunk in enumerate(chunks, start=1):
        filename = f'post{idx}.txt'
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(chunk)
        print(f'Written {filename}')


except Exception as e:
    print(e)

'''
with open('reddit_posts.csv', mode='w', newline='', encoding='utf-8') as file:
    writer = csv.writer(file)
    writer.writerow(['URL', 'Title', 'Views', 'Likes'])  # Header row

    for url in URLS:
        try:
            source = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
            source.raise_for_status()

            soup = BeautifulSoup(source.text, 'html.parser')

            # Get title
            h1_tag = soup.find('h1', class_="font-semibold")
            if h1_tag:
                title = h1_tag['aria-label'].replace('Post Title: ', '')
            else:
                title = "Title Not Found"

            # Instagram API placeholder
            # You need to get real views and likes via Instagram API
            views = 'INSTAGRAM_VIEWS'  
            likes = 'INSTAGRAM_LIKES'

            # Write to CSV
            writer.writerow([url, title, views, likes])
            print(f'Successfully written: {title}')

        except Exception as e:
            print(f'Error processing {url}: {e}')
'''