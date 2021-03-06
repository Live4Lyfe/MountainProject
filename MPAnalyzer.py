# -*- coding: utf-8 -*-
"""
Created on Mon Dec 17 19:51:21 2018

@author: Bob
"""

from sklearn.preprocessing import StandardScaler
from sklearn.cluster import DBSCAN
from nltk.tokenize import word_tokenize
from nltk.stem import PorterStemmer
from nltk.corpus import stopwords
from sqlalchemy import create_engine
from config import config
import pandas as pd
import numpy as np
import unidecode
import psycopg2
import re
import click
from tqdm import tqdm
from mpproj.routefinder.StyleInformation import *


def MPAnalyzer():
    '''Finishes cleaning routes using formulas that require information about
    the whole database.

    The Bayesian rating system, route clustering algorithm and calculation of
    TFIDF values require information about all routes, and not just one that is
    of interest.  Therefore, this file must be run after all data collection
    has finished. This function is a handler for six functions:
        - bayesian_rating: Calculates the weighted quality rating for each
            route
        - route_clusters: Groups routes together based on geographic distance
        - idf: Calculates inverse-document-frequency for words in the route
            descriptions
        - tfidf: Calclates term-frequency-inverse-document-frequency for words
            in route descriptions
        - normalize: Normalizes vectors for TFIDF values
        - find_route_styles: Compares routes to the ideal to help categorize

    Returns:
        Updated SQL Database
    '''

    print('Connecting to the PostgreSQL database...', end='')
    engine = create_engine(
        'postgresql+psycopg2://postgres:postgres@localhost:5432/routes')
    params = config.config()
    conn = psycopg2.connect(**params)
    cursor = conn.cursor()
    print('Connected')
    tqdm.pandas()

    def tfidf(min_occur=0.001, max_occur=0.9):
        ''' Calculates Term-Frequency-Inverse-Document-Frequency for a body of
        documents.

        Term-Frequency-Inverse-Document-Frequency(TFIDF) is a measure of the
        importance of words in a body of work measured by how well they help to
        distinguish documents.  Words that appear frequently in documents score
        high on the Term-Frequency metric, but if they are common across the
        corpus, they will have low Inverse-Document-Frequency scores.  TFIDF
        can then be used to compare documents to each other, or, in this case,
        to documents with known topics.

                                   TFIDF = TF * IDF

                          TF = Term Frequency
                          IDF = Inverse Document Frequency

        Args:
            min_occur(int): The minimum number of documents that a word has to
                appear in to be counted. Included to ignore words that only
                appear in a few documents, and are therefore not very useful
                for categorization.
            max_occur(int): The maximum number of documents that a word can
                appear in to be counted.  This is included to ignore highly
                common words that don't help with categorization.

        Returns:
            routes(pandas Dataframe): Holds route-document information,
                including term-frequency, inverse-document-frequency, TFIDF,
                and normalized TFIDF values
            Updated SQL Database: Updates the TFIDF table on main DB with the
                routes dataframe
        '''

        print('Getting number of routes', end=' ', flush=True)
        cursor.execute('SELECT COUNT(route_id) FROM Routes')
        num_docs = cursor.fetchone()[0]
        print(num_docs)

        print('Getting route text data', flush=True)        
        min_occur *= num_docs
        max_occur *= num_docs
        query = 'SELECT route_id, word, tf FROM Words'
        routes = pd.read_sql(query, con=conn, index_col='route_id')

        print('Removing non-essential words.', flush=True)
        routes = routes.groupby('word', group_keys=False)
        routes = routes.progress_apply(
            weed_out,
            min_occur=min_occur,
            max_occur=max_occur)\
                       .set_index('route_id')

        print('Getting IDF', flush=True)
        routes = routes.groupby('word', group_keys=False)
        routes = routes.progress_apply(
            idf,
            num_docs=num_docs).set_index('route_id')

        print('Calculating TFIDF', flush=True)
        routes['tfidf'] = routes['tf'] * routes['idf']

        print('Normalizing TFIDF values', flush=True)
        routes = routes.groupby(routes.index, group_keys=False)
        routes = routes.progress_apply(lambda x: normalize('tfidf', table=x))

        print('Writing TFIDF scores to SQL', flush=True)
        routes = routes.set_index('route_id')
        routes = routes[['word', 'idf', 'tfidfn']]

        # This will take a long time
        routes.to_sql('TFIDF', con=engine, if_exists='replace', chunksize=1000)

    def weed_out(table, min_occur, max_occur):
        '''Removes words that are too common or too rare
        
        Args:
            table(Series): Instances of a word
            min_occur: Fewest number acceptable
            max_occur: Greatest number acceptable
            
        Returns:
            table: updated series'''
        
        if min_occur < len(table) < max_occur:
            return table.reset_index()

    def idf(word, num_docs):
        ''' Finds inverse document frequency for each word in the selected
        corpus.

        Inverse document frequency(IDF) is a measure of how often a word
        appears in a body of documents.  The value is calculated by:

                            IDF = 1 + log(N / dfj)

             N = Total number of documents in the corpus
             dfj = Document frequency of a certain word, i.e., the number of
                 documents that the word appears in.

        Args:
            word(pandas dataframe): A dataframe composed of all instances of a
                word in a corpus.
            num_docs(int): The total number of documents in the corpus

        Returns:
            word(pandas dataframe): The same document with the calculated IDF
                score appended.
        '''

        word['idf'] = 1 + np.log(num_docs / len(word))
        return word.reset_index()

    def normalize(*columns, table, inplace=False):
        ''' Normalizes vector length.

        Vector values must be normalized to a unit vector to control for
        differences in length.  This process is done by calculating the length
        of a vector and dividing each term by that value.  The resulting
        'unit-vector' will have a length of 1.

        Args:
            table(pandas dataframe): Table hosting vector to be normalized
            *columns(str): Names of columns to be normalized
            inplace(Boolean, default = False):
                If inplace=False, adds new columns with normalized values.
                If inplace=True, replaces the columns.

        Returns:
            table(pandas dataframe): Updated dataframe with normalized values.
        '''
        for column in columns:
            if not inplace:
                column_name = column + 'n'
            elif inplace:
                column_name = column

            length = np.sqrt(np.sum(table[column] ** 2))
            table[column_name] = table[column] / length
        return table.reset_index()

    def fill_null_loc():
        """Fills empty route location data.
        
        Not all routes have latitude and longitude coordinates, so we must use
        the coordinates of their parent area instead as a rough estimate.  This
        function first grabs all routes with no data, then fills in the data
        with the lowest level area it can, going up as many areas as needed
        until it finds one with proper coordinates.
        
        Returns:
            Updated SQL Database
            """
        print('Filling in empty locations', flush=True)
        # Select a route without location data
        cursor.execute('''
            SELECT route_id, area_id, name FROM Routes
            WHERE latitude is Null OR longitude is Null
            LIMIT 1''')
        route = cursor.fetchone()
        
        while route is not None:
            # Route ID
            rid = route[0]
            # From ID
            fid = route[1]
            name = route[2]
            print(f'Finding location information for {name}')
    
            # Loops until it finds proper data
            lat, long = None, None
            while lat == None or long == None:
                # Gets latitude and longitude from parent area
                cursor.execute(f'''
                    SELECT
                        latitude,
                        longitude,
                        from_id
                    FROM Areas
                    WHERE id = {fid}
                    LIMIT 1''')
                loc = cursor.fetchone()
                lat, long = loc[0], loc[1]
                fid = loc[2]
            # Updates DB
            cursor.execute(f'''
                UPDATE Routes
                SET
                    latitude = {lat},
                    longitude = {long}
                WHERE route_id = {rid}''')
            conn.commit()
            cursor.execute('''
                SELECT
                    route_id,
                    area_id,
                    name
                FROM Routes
                WHERE
                    latitude is Null
                    OR longitude is Null
                LIMIT 1''')
            route = cursor.fetchone()

    def route_clusters(routes):
        ''' Clusters routes into area groups that are close enough to travel
        between when finding climbing areas.

        Routes can be sorted into any number of sub-areas below the 'region'
        parent. By clustering the routes based on latitude and longitude
        instead of the name of the areas and parent areas, the sorting
        algorithm will be able to more accurately determine which routes are
        close together. This function uses SciKit's Density Based Scan
        clustering algorithm. The algorithm works by grouping points together
        in space based on upper-limits of distance and minimum numbers of
        members of a cluster. More generally, the algorithm first finds the
        epsilon neighborhood of a point. This is the set of all points whose
        distance from a given point is less than a specified value epsilon.
        Then, it finds the connected core-points, which are the points that
        have at least the minimum number of connected points in its
        neighborhood. Non-core points are ignored here.  Finally, the
        algorithm assigns each non-core point to a nearby cluster if is within
        epsilon, or assigns it to noise if it is not.

        The advantages of this is that the scan clusters data of any shape, has
        a robust response to outliers and noise, and that the epsilon and min
        points variables can be adjusted.

        This function returns the label/name for the cluster that a route
        appears in, as well as the number of other routes in that same cluster.
        This will allow the sorting algorithm to more heavily weight routes
        that are clustered near others.

        Args:
            routes(pandas df): Pulled from cleaned route SQL DB with columns:
                - route_id (int, unique): Unique route identifies
                - latitude (float)
                - longitude (float)
        Returns:
            routes(pandas df): Updated with clustered area group number:
                - route_id (int, unique): Unique route identifies
                - area_group (int): Cluster id
        '''

        # Route location
        lats = routes['latitude']
        longs = routes['longitude']
        locs = []
        for x in range(len(lats)):
            locs.append((lats.iloc[x], longs.iloc[x]))

        # Converted into df
        locs = StandardScaler().fit_transform(locs)
        # Max distance in latitude
        epsilon = 0.0007
        # Min number of routes in a cluster
        min_routes = 3
        # Distance baced scan
        db = DBSCAN(eps=epsilon, min_samples=min_routes).fit(locs)
        core_samples_mask = np.zeros_like(db.labels_, dtype=bool)
        core_samples_mask[db.core_sample_indices_] = True
        # Cluster names
        labels = db.labels_
        unique, counts = np.unique(labels, return_counts=True)
        counts = dict(zip(unique, counts))
        # Number of routes in the same cluster as a given route
        area_counts = []
        
        for label in labels:
            if label >= 0:
                # Counts number of routes
                area_counts.append(counts[label])
            # Areas are given a cluster id of -1 if the are not part of a
            # cluster
            elif label == -1:
                # If so, there is only 1 route in their 'cluster'
                area_counts.append(1)

        routes['area_group'] = labels
        routes['area_counts'] = area_counts
        routes = routes[['area_group', 'area_counts']]
        return routes

    def bayesian_rating(routes):
        ''' Updates route quality with weighted average.

        The Bayesian average rating system helps to mitigate the effects of
        user ratings for routes that only have a few reviews.  The weighted
        rating works by first finding the average rating for all routes, and
        using that to bring low-rated routes up and high-rated routes down.
        The result - the Bayes rating - is an updated rating weighted by the
        average number of stars across all routes.  The weight decreases
        according to the number of votes cast.

                Bayesian rating = (r * v) + (a * 10) / (v + 10)
                
                    r = Route rating
                    v = Number of votes
                    a = Average rating across all routes

        Essentially, the function gives each route phantom-users who all give
        the route the average score.  For routes with a high number of ratings
        the effect of the additional phantom users is minimal, but for routes
        with only one or two actual user ratings, the effect is large.  This
        keeps 4-star rated routes from dominating the sorting algorithm if they
        only have a few votes, and helps promote unrated routes that may be of
        high quality.

        Args:
            routes(pandas df): Pulled from cleaned route SQL DB with columns:
                - route_id (int, unique): Unique route identifiers
                - stars (float): Raw average rating
                - votes (int): Number of user ratings
        Returns:
            routes(pandas df): Updated dataframe with Bayes rating and columns:
                - route_id (int, unique): Unique route identifies
                - bayes (float): Weighted average rating
        '''

        # Average rating of all routes
        stars = pd.read_sql('SELECT stars FROM Routes', con=conn)
        avg_stars = np.mean(stars)['stars']
        # Weighted Bayesian rating
        routes['bayes'] = round((((routes['votes'] * routes['stars'])
                            + avg_stars * 10) / (routes['votes'] + 10)), 1)
        return routes['bayes'].to_frame()
    
    def find_route_styles(*styles, path='Descriptions/'):
        ''' Returns weighted scores that represent a route's likelihood of
        containing any of a series of features, e.g., a roof, arete, or crack.
    
        Route names, descriptions, and user comments can indicate the presence
        of rock and route features. Term-Frequency-Inverse-Document-Frequency
        (TFIDF) values for the blocks of text gathered for each route can be
        compared to 'archetypal' routes to glean insight into these features.
        This comparison is further clarified using Bayesian statistics to
        measure the credibility of the comparision, and is then adjusted to
        reflect that.  At present, each route is compared against archetypal
        routes with the following features:
    
            Aretes - A sharp vertical edge of a block, cliff or boulder
            Chimney - A large vertical crack that a climber can fit in and
                climb using opposing pressure
            Crack - Smaller cracks ranging from finger-sized to a few inches
                wide (off-width)
            Slab - Low-angle rock faces (less than vertical)
            Overhang - Roofs, caves or more-than-vertical rock faces
    
        More styles or archetypes can be added in the future by creating .txt
        files and adding them to the 'Descriptions' sub-folder, then adding the
        style to the *styles argument.
    
        Args:
            *styles(str): The name of the files that each route will be
                compared against.
            path(str): Folder location of the Database
    
        Returns:
            Updated SQL Database with weighted route scores
        '''
    
        def text_splitter(text):
            '''Splits text into words and removes punctuation.
    
            Once the text has been scraped it must be split into individual
            words for further processing.  The text is all put in lowercase,
            then stripped of punctuation and accented letters. Tokenizing helps
            to further standardize the text, then converts it to a list of
            words. Each word is then stemmed using a Porter stemmer.  This
            removes suffixes that make similar words look different, turning,
            for example, 'walking' or 'walked' into 'walk'.  Stop words are
            also filtered out at this stage.
    
            Args:
                text(str): Single string of text to be handled
    
            Returns:
                text(list): List of processed words.'''
    
            # Converts to lowercase
            text = text.lower()
            # Strips punctuation and converts accented characters to unaccented
            text = re.sub(r"[^\w\s]", '', text)
            text = unidecode.unidecode(text)
            # Tokenizes words and returns a list
            text = word_tokenize(text)
            # Remove stopwords            
            stop_words = set(stopwords.words('english'))
            # Stems each word in the list
            ps = PorterStemmer()
            text = [ps.stem(word) for word in text if word not in stop_words]
    
            return text
    
        def archetypal_tf(*styles, path):
            ''' Returns term-frequency data for descriptions of archetypal
            climbing routes and styles.  This will be used later to categorize
            routes.
    
                            Term-Frequency = t / L
    
                    t = Number of appearances for a word in a document
                    L = Number of total words in the document
    
            Args:
                *styles(str): Name of .txt file to parse.  Can either be the
                    plain name or have the .txt suffix
                path(str): Path to folder with route descriptions
            Returns:
                tf.csv(CSV File): CSV File of term frequency for each style.
                    This will help determine if TF values are what is expected
                    when adding new styles.
                archetypes(Pandas Dataframe): Holds words term-frequency values
                    for words in the files.'''
    
            # Initializes Dataframe
            archetypes = pd.DataFrame()
            for style in styles:
                # Formats suffix
                if style.endswith('.txt'):
                    # Opens .txt file
                    try:
                        file = open(path + style)
                        style = style[:-4]
                    # Returns errors
                    except OSError as e:
                        return e
                else:
                    try:
                        file = open(path + style + '.txt')
                    except OSError as e:
                        return e
    
    
                # Creates single block of text
                text = ''
                for line in file:
                    text += line
                # Splits and processes text
                text = text_splitter(text)
    
                # Length of document in words
                length = len(text)
                # Counts appearances of each word
                text = pd.DataFrame({'word': text})['word']\
                         .value_counts()\
                         .rename('counts')\
                         .to_frame()
    
                # Calculates Term-Frequency
                text[style] = text['counts'].values / length
                text = text[style]
    
                # Creates master Dataframe of Termfrequency data for each style
                archetypes = pd.concat([archetypes, text], axis=1, sort=True)
            archetypes.to_csv(path + 'TF.csv')
            return archetypes
    
        def archetypal_idf(words):
            ''' Findes inverse document frequency (IDF) for each word in the
            archetypal style documents.
    
            The archetypal documents should not be included in the calculation
            of IDF values, so this function just pulls the IDF values from the
            database after they are calculated. IDF is a measure of how often a
            word appears in a body of documents. The value is calculated by:
    
                                IDF = 1 + log(N / dfj)
    
                 N = Total number of documents in the corpus
                 dfj = Document frequency of a certain word, i.e., the number
                     of documents that the word appears in.
    
            Args:
                word(list): All unique words in all the archetype documents
    
            Returns:
                archetypes(pandas dataframe): IDF values for each word pulled
                    from the Database.'''
    
            # Formats query to include list of unique words
            query = f'''
                SELECT
                    DISTINCT(word),
                    idf
                FROM "TFIDF"
                WHERE word IN {words}'''
            # Pulls SQL data into Pandas dataframe
            archetypes = pd.read_sql(query, con=conn, index_col='word')
    
            return archetypes
    
        def get_routes(route_ids=None):
            '''Creates Pandas Dataframe of normalized TFIDF values for each
            word in each route description.
    
            Args:
                route_ids: Optional.  Allows for a slice to be parsed.
            Returns:
                routes(Pandas Series): MultiIndex series with indexes
                'route_id' and 'word' and column 'tfidfn' - Normalized TFIDF'''
    
            # Pulls route_id, word, and normalized TFIDF value
            if route_ids is None:
                query = '''
                    SELECT
                        route_id,
                        word,
                        tfidfn
                    FROM "TFIDF"'''
            else:
                route_ids = tuple(route_ids)
                query = f'''
                    SELECT
                        route_id,
                        word,
                        tfidfn
                    FROM "TFIDF"
                    WHERE route_id in {route_ids}'''
    
            # Creates Pandas Dataframe
            routes = pd.read_sql(
                query,
                con=engine,
                index_col=['route_id', 'word'])
            routes = routes.squeeze()

            return routes
    
        def get_word_count(route_ids=None):
            '''Finds length of route description in words.
    
            Args:
                route_ids: Optional. Allows for a slice to be parsed
            Returns:
                word_count(Pandas dataframe): Dataframe with index route_id and
                    column 'word_count' - length of a route description in
                    words'''
    
            # Pulls route_id and word_count for each route
            if route_ids is None:
                query = 'SELECT route_id, word_count FROM Words'
            else:
                route_ids = tuple(route_ids)
                query = f'''
                    SELECT
                        route_id,
                        word_count
                    FROM Words
                    WHERE route_id in {route_ids}'''
    
            # Calculates document length
            word_count = pd.read_sql(query,
                                     con=conn,
                                     index_col='route_id').groupby(level=0)

            # We will take the log of the word count later, so we cannot leave
            # zeroes in the series
            word_count = word_count.progress_apply(lambda x: np.sum(x) + 0.01)
            word_count.fillna(0.01, inplace=True)
            
            return word_count
    
        def cosine_similarity(route, archetypes):
            '''Compares routes to archetypes to help categorize route style.
    
            Cosine similarity is the angle between two vectors.  Here, the
            normalized TFIDF values for each word in the route description and
            archetype documents serve as the coordinates of the vector. Finding
            the cosine similarity is therefore simply their dot-product.
    
                    Cosine Similarity = Σ(ai * bi)
    
                    ai = TFIDF for a word in the route description
                    bi = TFIDF for the same word in the archetype document.
    
            The similarity will range between 0 and 1, 1 being identical and 0
            having no similarity.
    
            Args:
                route(Pandas dataframe): MultiIndex frame with indexes route_id
                    and word and columns normalized TFDIF values
                archetypes(Pandas dataframe): Frame with index word and columns
                    normalized TFIDF values.
    
            Returns:
                terrain(Pandas dataframe): Frame with columns for each style,
                    holding cosine simlarity values.'''

            try:
                rid = route.index[0][0]
            except:
                return

            route = archetypes.multiply(route, axis=0)
            terrain = pd.DataFrame(index=[rid])

            for column in route:
                cosine_sim = np.sum(route[column])
                terrain[column] = cosine_sim

            return terrain

    
        def score_routes(*styles, word_count, path, routes):
            '''Gets TF, IDF data for archetypes, then finds TFIDF and cosine
            similarity for each route/style combination.
    
            Finding the raw cosine similarity scores requires the functions
            archetypal_tf, archetypal_idf, and normalize.  This function helps
            organize the retrieval and processing of the data for those functions.
    
            Args:
                word_count(Pandas dataframe): Dataframe with index route_id and
                    column 'word_count' - length of a route description in
                    words
            Returns:
                TFIDF.csv(CSV file): TFIDF for each word in each style.  This
                    helps users determine if the TFIDF values are what they
                    would expect when adding new styles.
                routes(Pandas dataframe): Holds cosine similarity for each
                    route/style combination'''

            if click.confirm('Rescore archetypes?'):
                # Gets Term-Frequency data for words in archetype documents
                archetypes = archetypal_tf(*styles, path=path)
                # Gets list of unique words in archetype documents
                words = tuple(archetypes.index.tolist())
                # Gets IDF Values for those words from the Database
                idf = archetypal_idf(words)
                # Selects words for archetype documents that have a correpsonding
                # IDF value in the database
                archetypes = archetypes[archetypes.index.isin(idf.index)]
        
                # Multiplies TF by IDF values to get TFIDF score
                archetypes = archetypes.mul(idf['idf'], axis=0)
                # Normalizes TFIDF scores
                archetypes = normalize(
                    table=archetypes,
                    inplace=True,
                    *styles)
                archetypes = archetypes.rename(
                    columns={'index': 'word'}
                    ).set_index('word')
                
                # Writes to CSV
                archetypes.to_csv(path + 'TFIDF.csv')
    
            archetypes = pd.read_csv(path + 'TFIDF.csv', index_col='word')
    
            # Groups words by route_id, then finds cosine similarity for each
            # route-style combination
            routes = routes.groupby('route_id').progress_apply(
                cosine_similarity,
                archetypes=archetypes)
            # Reformats routes dataframe
            routes.index = routes.index.droplevel(1)
            routes = pd.concat([routes, word_count], axis=1, sort=False)
            routes.fillna(0, inplace=True)

            return routes
    
        def weighted_scores(*styles, table, inplace=False):
            '''Weights cosine similarity based on credibility.
    
            The cosine similarity between a route and a style archetype
            measures how close the two documents are.  Depending on the score
            and the word count of the route, however, this score can be more or
            less believable.  Using Bayesian statistics helps weight the scores
            based on the credibility.
    
            We can plot word count and cosine similarity in two dimensions.
            Normalizing each so that the maximum value is one results in a
            plane with four edge cases:
    
                            cosine similarity | word count
                                    0               0
                                    1               0
                                    0               1
                                    1               1
        
            When both word count and cosine similarity is high, the
            believability of the cosine score is at its highest.  This is
            analagous to a route that scores well with the 'overhang' document,
            therefore mentioning words like 'overhang' or 'roof' frequently,
            that also has a lot of words.
    
            If the word count is high and the cosine similarity is low the
            believability of the score is high, but not as high as before.
            This is analagous to a route that never mentions words associated
            with 'overhang' despite a high word count.  We can be reasonably
            sure in this case that the route does not have an overhang.
    
            If the word count of a route is low but the cosine score is high,
            we can be reasonably sure that the score is somewhat accurate. This
            is a result of a route called, for instance, 'Overhang Route'.
            Despite the low word count, it is highly likely that the route has
            an overhang on it.
    
            Finally, for routes that have both low word count and cosine score,
            we have no way to be sure of the presence (or absence) of a
            feature.  In this case, our best guess is that the route is at
            chance of featuring a given style of climbing.
    
            If we chart word count, cosine similarity, and the credibility of
            the cosine score, we are left with a cone with a point at the
            origin, reaching up at a 45 degree angle along the credibility (z)
            axis. Each route will exist somewhere on the surface of the cone.
            To make use of this, we need to calculate this position. The height
            to the cone gives us the credibility, and can be calculated with:
    
                    Credibility = sqrt(W ** 2 + C ** 2) * tan(45 degrees)
    
            Since tan(45 degrees) is 1, this simplifies to:
    
                            Credibility = sqrt(W ** 2 + C ** 2)

                               W = Word count
                               C = Cosine similarity
    
            The credibility of a route's score can be fed back into the score
            to find a weighted route score.  As the word count and cosine score
            get close to zero, the average score should play more of a role in
            the outcome. Therefore:
    
    
                Score = C * sqrt(W ** 2 + C ** 2) + (1 - C)(1 - W) * Cm
    
                                W = word count
                                C = cosine Similarity
                                Cm = Average cosine similarity across routes
    
            Finally, the scores are processed with a Sigmoid function,
            specifically the logistic function.
            
                            f(x) = L / 1 + e^(-k(x-x'))
                            
                                L = upper bound
                                e = Euler's constant
                                k = logistic growth rate
                                x' = Sigmoid midpoint
            
            By manipulating the constants in this function, we can find a
            continuous threshold-like set of values that are bounded by 0 and
            1.  The midpoint of the threshold is the mean value of the scores
            plus one standard devaition.  Therefore, the function used here is:

                            f(x) = 1 / (1 + e^(-100(x - x'))

                                x' = mean + sigma
                                e = Euler's constant

    
            Args:
                *styles(str): Names of the style archetypes
                table(Pandas dataframe): Master dataframe of cosine scores for
                    each route
                inplace(Boolean, default = False):
                    If inplace=False, adds new columns with weighted values.
                    If inplace=True, replaces the columns.
                    
            Returns:
                Updated SQL Database'''
    
            # Gets name for the columns to write data
            if inplace:
                count = 'word_count'
            else:
                count = 'word_count_norm'
    
            # As the word count increases, the credibility increases as a
            # logarithmic function
            table[count] = np.log10(table['word_count'])

            table_min = table[count].min()
            table_max = table[count].max()
            table_diff = table_max - table_min

            table[count] = (table[count].values - table_min) / table_diff
    
            # Gets weighted scores for each style
            for style in styles:
                # Stores name to write data on
                if inplace:
                    column_name = style
                else:
                    column_name = style + '_weighted'
    
    
                # Find average cosine similarity across routes
                style_avg = table[style].mean()
                # Calculate weighted rating
                table[column_name] = (
                    table[style].values * np.sqrt(
                        table[style].values ** 2 + table[count].values ** 2)
                    + (1 - table[count].values) * (1 - table[style].values)
                    * style_avg)

                threshold = table[column_name].mean() + table[column_name].std()
                # Calculates final score using Sigmoid function
                table[column_name] = (
                    1 / (1 + np.e ** (-100 *
                                        (table[column_name]
                                    - threshold))))

            return table
    
        # Run functions

        print('Getting route information')
        routes = get_routes()

        print('Getting word count')
        word_count = get_word_count()

        print('Scoring routes')
        routes = score_routes(
            *styles,
            word_count=word_count,
            path=path,
            routes=routes)
        
        print('Getting weighted scores')
        routes = weighted_scores(*styles, table=routes, inplace=True)
        
        # Collects the full database
        query = 'SELECT * FROM Routes'
        all_routes = pd.read_sql(query, conn, index_col='route_id')
        
        # Combines columns in the routes dataframe with the full database if
        # they don't already exist in the full database
        updated = pd.concat(
            [routes[
                ~routes.index.isin(all_routes.index)],
                all_routes],
            sort=False)
    
        updated.update(routes)

        updated.rename_axis('id', inplace=True)
        
        for i in range(5):
            feature = terrain_types[i]
            other_features = terrain_types[:i] + terrain_types[i+1:]
            other_features = updated[other_features]
            updated[feature+'_diff'] = updated[feature] * (
                                            updated[feature]
                                            - other_features.sum(axis=1))

        # Write to Database        
        updated.to_sql(
            'Routes_scored',
            con=engine,
            if_exists='replace')
        
        return

    def get_links(route, route_links):
        """Gets links between a route and all parent areas
        
        Args:
            route(Series): Route information
            route_links(Series): Links between areas
        Returns:
            Updated SQL"""
        
        
        route_id = route.name
        
        parents = [route.squeeze()]
        base = False
        
        while not base:
            try:
                grandparent = route_links.loc[parents[-1]]['from_id']
                parents.append(grandparent)
            except:
                base = True
                
        parents = pd.DataFrame({
            'id': route_id,
            'area': parents,
            })
            
        parents = parents.dropna(how='any')
                
        parents.to_sql(
            'route_links',
            con=engine,
            if_exists='append',
            index=False)
        
        
    def get_children(area):
        """Gets area children for all areas
        
        Args:
            area(Series): area information
            
        Returns:
            Updated SQL"""
            
        # Checks if child area in area DB
        try:
            children = area_links.loc[area]
        # If not, this is a base level area
        except:
            return
        
        if type(children) is np.int64:
            
            children = pd.Series(
                    data=children,
                    index=[area],
                    name='id')
            children.index.name = 'from_id'
    
        for child in children:
            grandchildren = get_children(child)
            if grandchildren is not None:
                grandchildren.index = [area] * len(grandchildren)
                children = pd.concat([children, grandchildren])
        
        return children
    
    def get_area_details(*styles):
        """Gets route data for each area and creates a summary.
        
        Args:
            styles: terrain styles
        
        Returns:
            Updated SQL
        """
    
        routes = pd.read_sql("""
           SELECT *
           FROM routes_scored""",
           con=engine,
           index_col='id')
        
        terrain_info = routes[terrain_types]
        
        average_stars = routes.stars.mean()
        other = ['alpine', 'pitches', 'length', 'danger_conv']
        
        def grade_areas():
            routes_in_area = pd.read_sql("""
                 SELECT *
                 FROM route_links""",
                 con=engine,
                 index_col='area').squeeze()
            routes_in_area = routes_in_area.groupby(routes_in_area.index)
            
            def area_styles_and_grades(area):
                area_routes = routes.loc[area]
                
                style = area_routes[climbing_styles+other].mean()
                
                grade = area_routes[grades].mean().round()
                
                grade_std = area_routes[grades].std() + grade
                grade_std = grade_std.round()
        
                grade_std.index = grade_std.index + '_std'
                
                score_total = (area_routes.stars * area_routes.votes).sum()
                votes_total = area_routes.votes.sum()
                
                bayes = (score_total + 10 * average_stars) / (votes_total + 10)
                area_information = style.append(grade)
                area_information = area_information.append(grade_std)
                area_information['bayes'] = bayes            
                
                area_information = area_information.to_frame().transpose()
                
                return area_information
                
            
            def get_conversion(area):
                while True:
                    if area.sport or area.trad or area.tr:
                        
                        score = area.rope_conv
                        try:
                            score = int(area.rope_conv)
                        except:
                            break
                    
                        score_std =  area.rope_conv_std
                        if score_std == score_std:
                            score_std = int(area.rope_conv_std)
                        else:
                            score_std = score
                                        
                        for system in rope_systems:
                            if score_std >= len(system_to_grade[system]):
                                score_std = -1        
        
                            area[system] = system_to_grade[system][score]
                            area[system+'_std'] = system_to_grade[system][score_std]
                    else:
                        area.rope_conv = None
                        area.rope_conv_std = None
                    break
                        
                while True:
                    if area.boulder:
                        score = area.boulder_conv
                        try:
                            score = int(score)
                        except:
                            break 
                
                        score_std = area.boulder_conv_std
                        if score_std == score_std:
                            score_std = int(score_std)
                        else:
                            score_std = score
                                            
                        for system in boulder_systems:
                            if score_std >= len(system_to_grade[system]):
                                score_std = -1        
                            area[system] = system_to_grade[system][score]
                            area[system+'_std'] = system_to_grade[system][score_std]
                    else:
                        area.boulder_conv = None
                        area.boulder_conv_std = None
                    break
                 
                for system, data in misc_system_to_grade.items():
                    if area[system]:
                        score = area[data['conversion']]
                        score_std = area[data['conversion'] + '_std']
                        try:
                            score = int(score)
                        except:
                            continue
            
                        if score_std == score_std:
                            score_std = int(score_std)
                        else:
                            score_std = score
            
                        if score_std >= len(data['grades']):
                            score_std = -1       
        
            
                        area[data['rating']] = data['grades'][score]
                        area[data['rating']+'_std'] = data['grades'][score_std]
                                    
                return area
            
            def get_grades():
                # Get grades for each area
                print('Getting routes in area')
                area_information = routes_in_area.progress_apply(
                    area_styles_and_grades)
                area_information.index = area_information.index.droplevel(1)
                area_information.index = area_information.index.rename('id')
                print('Getting area information')
                area_information = area_information.progress_apply(
                    get_conversion, axis=1)
                
                areas = pd.read_sql(
                    'SELECT * FROM areas',
                    con=engine,
                    index_col='id')
                
                areas = areas.update(grades)
                
                print(areas)
                
#                areas.to_sql(
#                    'areas',
#                    if_exists='replace',
#                    con=engine) 
                
            def area_terrain(area):   
                
                num_routes = len(area)
    
                area_terrain_info = terrain_info.loc[area].quantile(.95)
                area_terrain_info = area_terrain_info / area_terrain_info.max()
                area_terrain_info = area_terrain_info.to_frame().transpose()
    
                for i in range(5):
                    feature = terrain_types[i]
                    other_features = terrain_types[:i] + terrain_types[i+1:]
                    other_features = area_terrain_info[other_features]
    
    
                    area_terrain_info[feature+'_diff'] = (
                        area_terrain_info[feature] 
                        * (area_terrain_info[feature] - other_features.sum(axis=1))
                        * np.log(num_routes + np.e))
                return area_terrain_info
            
            def get_terrain():
                
                area_terrain_info = routes_in_area.progress_apply(area_terrain)
                area_terrain_info.index = area_terrain_info.index.droplevel(1)
                area_terrain_info.index.rename('id', inplace=True)
                
                for terrain_type in terrain_types:
                    area_terrain_info[terrain_type + '_diff'] = (
                        (area_terrain_info[terrain_type + '_diff']
                            - area_terrain_info[terrain_type + '_diff'].min())
                        / (area_terrain_info[terrain_type + '_diff'].max()
                            - area_terrain_info[terrain_type + '_diff'].min()))
                
                area_terrain_info.index = area_terrain_info.index.astype('int32')
                area_terrain_info.dropna(inplace=True)
                
                areas = pd.read_sql('SELECT * FROM areas', con=engine, index_col='id')
                areas = areas.update(terrain)
                
                print(areas)
#                areas.to_sql(
#                    'areas',
#                    if_exists='replace',
#                    con=engine)
                            
            get_grades()
            get_terrain()
                
            
        def get_base_areas():                
            cursor.execute('''
               SELECT id
               FROM areas
               WHERE
                   name = 'International' AND
                   from_id is Null''')
            
            international_id = cursor.fetchone()[0]
            
            cursor.execute(f"""
                SELECT id
                FROM areas
                WHERE
                    from_id = {international_id}""")
            country_ids = cursor.fetchall()
            
            country_ids = [
                country_id for sublist in country_ids for country_id in sublist]
            
            
            base_areas = [international_id] + [
                country_id for country_id in country_ids]
            
            return tuple(base_areas)
        
        def base_area_land_area():
            countries = pd.read_csv('country_land_data.csv', encoding='latin-1')
            countries.columns = ['name', 'land_area']
            countries.set_index('name', inplace=True)
            
            countries.land_area = countries.land_area.map(
                lambda x: re.findall("([\d.]+)", x)[0])
            countries.land_area = countries.land_area.astype('float64')
            
            states = pd.read_csv('state_land_data.csv')
            states.columns=['region', 'land_area']
            states.set_index('region', inplace=True)
            states.land_area = states.land_area.astype('int32')
            
            land_area = pd.concat([states, countries]).sort_values(by='land_area')
            return land_area        
    
        
        def update_sql(area):
            rating = area.squeeze()
            area_id = area.name
            
            cursor.execute(f'''
               UPDATE areas
               SET bayes = {rating}
               WHERE id = {area_id}''')
        
        def update_base_area_grades():
        
            base_areas = get_base_areas()
            base_areas = pd.read_sql(f"""
                SELECT
                    id,
                    name,
                    from_id
                FROM areas
                WHERE
                    from_id IN {base_areas} OR 
                    from_id IS NULL""",
                con=conn,
                index_col='name')
                
            base_areas = pd.concat(
                [base_areas, base_area_land_area()],
                axis=1,
                sort=True)
            base_areas = base_areas[
                ~base_areas.land_area.isna()
                & ~base_areas.id.isna()]
            base_areas.reset_index(inplace=True)
            base_areas.set_index('id', inplace=True)
            base_areas.index = base_areas.index.astype('int32')
            base_areas.land_area = base_areas.land_area.astype('int32')
            
            base_area_ids = tuple(base_areas.index.tolist())
            
            bayes = pd.read_sql(f"""
                SELECT
                    id,
                    bayes
                FROM areas
                WHERE id in {base_area_ids}""",
                con=engine,
                index_col='id')
            
            base_routes = pd.read_sql(f"""
                SELECT *
                FROM route_links
                WHERE area in {base_area_ids}""",
                con=engine,
                index_col='area')
                
            base_routes = base_routes.groupby(base_routes.index)
            base_routes = base_routes.apply(lambda x: len(x))
            base_routes.index = base_routes.index.astype('int32')
            base_routes.name = 'base_routes'
            
            base_areas = pd.concat([base_areas, bayes], axis=1)
            base_areas = pd.concat([base_areas, base_routes], axis=1)
            base_areas = base_areas[~base_areas.bayes.isna()]
            
            base_areas['density'] = base_areas.base_routes / base_areas.land_area
            base_areas.bayes = base_areas.bayes * base_areas.density
        
            base_areas.bayes = np.log(base_areas.bayes)
            base_areas.bayes = (
                4 * (base_areas.bayes - base_areas.bayes.min())
                / (base_areas.bayes.max() - base_areas.bayes.min()))
                
            base_areas = base_areas.sort_values(by='bayes')
            base_areas = base_areas['bayes'].to_frame()
                            
            base_areas.progress_apply(update_sql, axis=1)
            
            conn.commit()
            
            
        grade_areas()
        update_base_area_grades()

    # Fills in empty location data
    if click.confirm("Find location and rating data?"):
        fill_null_loc()

        print('Getting climbing area clusters', flush=True)
        cluster_text = '''
            SELECT route_id, latitude, longitude
            FROM Routes'''
        clusters = pd.read_sql(cluster_text, con=conn, index_col='route_id')
        clusters = route_clusters(clusters)

        print('Getting Bayesian rating', flush=True)
        # Gets Bayesian rating for routes
        query = '''SELECT route_id, stars, votes
                        FROM Routes'''
        bayes = pd.read_sql(query, con=conn, index_col='route_id')
        bayes = bayesian_rating(bayes)
        
        print('Writing to SQL',flush=True)
        # Combines metrics
        add = pd.concat([bayes, clusters], axis=1)
        # Writes to the database
        with click.progressbar(add.index) as bar:
            for route in bar:
                rate = add.loc[route]['bayes']
                group = add.loc[route]['area_group']
                cnt = add.loc[route]['area_counts']

                cursor.execute(f'''
                    UPDATE Routes
                    SET
                        bayes = {rate},
                        area_group = {group},
                        area_counts = {cnt}
                    WHERE route_id = {route}''')
        conn.commit()

    if click.confirm("Update TFIDF scores?"):
        tfidf()

    if click.confirm("Find route terrain scores?"):
        # Gets route scores for climbing styles
        find_route_styles('arete', 'chimney', 'crack', 'slab', 'overhang')
        
    if click.confirm("Get Route links"):
        query = "SELECT id, area_id FROM routes_scored"
        routes = pd.read_sql(query, con=conn, index_col='id')
            
        routes['area_id'] = routes['area_id'].astype('int32')
        query = "SELECT id, from_id FROM areas"
        route_links = pd.read_sql(query, con=conn, index_col='id')
        
        cursor.execute('''DROP TABLE route_links''')
        conn.commit()
    
        routes.progress_apply(
            get_links,
            args=(route_links,),
            axis=1)

    if click.confirm("Get area links"):
        query = "SELECT id, from_id FROM areas"
        area_links = pd.read_sql(query, con=engine)
        area_links = area_links.dropna()
        area_links = area_links.set_index('from_id').squeeze()
        
        area_links.index = area_links.index.astype('int32')
        
        all_children = pd.DataFrame()
        for area in tqdm(area_links.index.unique()):
            children = get_children(area)
            all_children = pd.concat([all_children, children])
            
            
        all_children.index.name = 'from_id'
        all_children.columns=['id']
        
        all_children.to_sql(
            'area_links',
            con=engine,
            if_exists='replace')
        
    if click.confirm('Update area terrain and scores'):        
        get_area_details('arete', 'chimney', 'crack', 'slab', 'overhang')
    
    print('Complete')


if __name__ == '__main__':
    MPAnalyzer()
